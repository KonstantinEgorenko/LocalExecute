"""Sequential opt-in local model timing diagnostics; never changes routing policy."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from .diagnostic_runtime_profiles import diagnostic_models_document
from .agent_loop.protocol import agent_action_json_schema
from .executor.client import OllamaNativeClient, OllamaTimeouts


@dataclass(frozen=True)
class Profile:
    profile_id: str
    model: str
    endpoint: str
    num_ctx: int
    num_gpu: int | None
    production_runtime: dict | None = None
    production_context: dict | None = None
    production_server: str | None = None
    effective_placement: dict | None = None


@dataclass(frozen=True)
class Workload:
    name: str
    input_tokens: int
    output_tokens: int

    def accepts(self, count):
        lo, hi = (256, 512) if self.name == "decode" else (self.input_tokens * .85, self.input_tokens * 1.15)
        return type(count) is int and lo <= count <= hi


WORKLOADS = (Workload("decode", 384, 512), Workload("swarm", 2000, 256),
             Workload("context_2k", 2048, 128), Workload("context_4k", 4096, 128), Workload("context_8k", 8192, 128))
METRICS = ("prompt_tokens_per_second", "generation_tokens_per_second", "wall_seconds")
STAT_METRICS = METRICS + ("prompt_eval_count", "eval_count", "prompt_eval_duration", "eval_duration")


class Transport(Protocol):
    def request(self, endpoint: str, path: str, payload: dict | None = None, *, timeout_seconds: float | None = None) -> dict: ...


class NativeTransport:
    def __init__(self, timeout=900):
        self.client = OllamaNativeClient(OllamaTimeouts(5, timeout, timeout))

    def request(self, endpoint, path, payload=None, *, timeout_seconds=None):
        timeout = self.client.timeouts.discovery_timeout_seconds if payload is None else self.client.timeouts.generation_timeout_seconds
        return self.client._request(endpoint, path, timeout_seconds=min(timeout, timeout_seconds) if timeout_seconds is not None else timeout, payload=payload)


def finite_positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def percentile(values, q):
    position = (len(values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def aggregate(values):
    values = sorted(v for v in values if finite_positive(v))
    if not values:
        return None
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if len(values) > 1 else None
    return {"count": len(values), "min": min(values), "median": statistics.median(values), "mean": mean,
            "max": max(values), "standard_deviation": sd, "cv_percent": sd / mean * 100 if sd is not None else None,
            "p10": percentile(values, .10), "p90": percentile(values, .90)}


def profiles_from_document(document):
    profiles = []
    for key, value in document["models"].items():
        runtime = value["runtime"]
        if runtime.get("backend") != "ollama":
            continue
        profiles.append(Profile(key, value["ollama_model"], document["servers"][value["server"]]["ollama_base_url"].rstrip("/"),
                                value["context"]["default"], runtime.get("num_gpu", 0 if runtime.get("execution") == "cpu" else None),
                                dict(value.get("configured_runtime", runtime)), dict(value.get("configured_context", value["context"])), value["server"],
                                value.get("effective_placement")))
    return tuple(profiles)


def profiles_from_runtime(root, mode, model_id=None, execution_profile=None):
    return profiles_from_document(diagnostic_models_document(root, mode, model_id, execution_profile))


class IsolationError(RuntimeError):
    """No further generation is safe until residency can be re-established."""


class SequentialBenchmark:
    def __init__(self, transport: Transport, profiles: tuple[Profile, ...], *, clock=time.perf_counter, save=None, isolation_endpoints=(),
                 unload_clock=time.monotonic, unload_sleep=time.sleep):
        self.transport, self.profiles, self.clock = transport, profiles, clock
        self.unload_clock, self.unload_sleep = unload_clock, unload_sleep
        self.endpoints = sorted({p.endpoint for p in profiles} | set(isolation_endpoints))
        for p in profiles:
            if urlparse(p.endpoint).hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("only configured loopback Ollama endpoints supported")
            if type(p.num_ctx) is not int or p.num_ctx <= 0:
                raise ValueError("invalid configured context")
            if p.num_gpu is not None and (type(p.num_gpu) is not int or p.num_gpu < -1):
                raise ValueError("invalid configured GPU layer setting")
        self.save = save or (lambda report: None)
        self.report = {"schema_version": 1, "diagnostic_only": True, "live": False, "models": [], "fatal_error": None,
                       "ranking_workload": "decode", "ranking_note": "Speed and stability only; no Swarm suitability decision.",
                       "capability_results": "NOT_RUN; use the independent model capability probe",
                       "methodology": {"cold": "model unloaded from Ollama before first timed request; OS/disk cache not cleared",
                           "warm": "resident model, changed prompt prefix to avoid reused KV prefix; no weight reload",
                           "warm_repetitions": 5, "extra_if_cv_gt_percent": 10, "extra_repetitions": 5,
                           "stdev": "sample (n-1)", "percentiles": "linear interpolation (n-1)*q",
                           "input_tolerance": "decode 256..512; other workloads +/-15%", "output_count_required": "exact requested cap",
                           "thinking": False, "rates": "server token counts / durations in nanoseconds; think=false"}}
        self.report["models"] = [{"profile": asdict(p), "status": "NOT_RUN", "workloads": [], "quantization": None} for p in self.profiles]
        self.serial = 0
        self.report["unload_diagnostics"] = []

    def residency_request(self, endpoint, path, payload, deadline):
        remaining = deadline - self.unload_clock()
        if remaining <= 0:
            raise IsolationError("model unload not confirmed within 10 seconds; benchmark stopped")
        response = self.transport.request(endpoint, path, payload, timeout_seconds=remaining)
        if self.unload_clock() > deadline:
            raise IsolationError("model unload not confirmed within 10 seconds; benchmark stopped")
        return response

    def residents(self, deadline=None):
        result = []
        for endpoint in self.endpoints:
            data = (self.transport.request(endpoint, "/api/ps") if deadline is None else
                    self.residency_request(endpoint, "/api/ps", None, deadline))
            if not isinstance(data, dict) or not isinstance(data.get("models"), list):
                raise IsolationError("cannot verify /api/ps residency")
            for item in data["models"]:
                if (not isinstance(item, dict) or
                        not isinstance(item.get("name", item.get("model")), str) or
                        not item.get("name", item.get("model")).strip() or
                        any(key in item and item[key] != item.get("name", item.get("model"))
                            for key in ("name", "model"))):
                    raise IsolationError("invalid resident model identity")
                result.append((endpoint, item))
        return result

    def empty(self):
        # One shared deadline includes requests and polling across all effective endpoints.
        started = self.unload_clock()
        deadline = started + 10.0
        record = {"model_requested": None, "endpoint": None, "unload_acknowledgement": None,
                  "poll_count": 0, "unload_elapsed_seconds": 0.0, "final_resident_set": None,
                  "residency_verified": False, "status": "CHECKING"}
        self.report["unload_diagnostics"].append(record)

        def snapshot():
            record["residency_verified"] = False
            items = self.residents(deadline)
            record["final_resident_set"] = [{**m, "endpoint": e} for e, m in items]
            record["residency_verified"] = True
            return items

        def identity(endpoint, item):
            return endpoint, item.get("name", item.get("model")), item.get("digest")

        try:
            pending = snapshot()
            # Initial residency is explicitly cleared by the live benchmark. Any new
            # identity appearing during this handshake is an isolation violation.
            allowed = {identity(e, m) for e, m in pending}
            for endpoint, item in pending:
                key = identity(endpoint, item)
                if key not in allowed:
                    continue  # Another requested unload also removed this resident.
                if record["model_requested"] is not None:
                    record["unload_elapsed_seconds"] = self.unload_clock() - started
                    record = {**record, "poll_count": 0, "unload_acknowledgement": None}
                    self.report["unload_diagnostics"].append(record)
                started = self.unload_clock()
                tag = item.get("name", item.get("model"))
                record.update(model_requested=tag, endpoint=endpoint, status="UNLOADING", residency_verified=False)
                ack = self.residency_request(endpoint, "/api/chat",
                    {"model": tag, "messages": [], "keep_alive": 0, "stream": False}, deadline)
                record["unload_acknowledgement"] = ack
                if (not isinstance(ack, dict) or ack.get("done") is not True or
                        ack.get("done_reason") != "unload" or ("model" in ack and ack["model"] != tag)):
                    raise IsolationError("invalid model unload acknowledgement")
                while True:
                    record["poll_count"] += 1
                    current = snapshot()
                    observed = {identity(e, m) for e, m in current}
                    if not observed <= allowed:
                        raise IsolationError("foreign resident appeared during unload")
                    allowed = observed
                    if key not in observed:
                        break
                    remaining = deadline - self.unload_clock()
                    if remaining <= 0:
                        raise IsolationError("model unload not confirmed within 10 seconds; benchmark stopped")
                    self.unload_sleep(min(.25, remaining))
                record["status"] = "EMPTY" if not current else "UNLOADED"
            if allowed:
                raise IsolationError("model unload not confirmed; benchmark stopped")
            record["status"] = "EMPTY"
        except Exception as exc:
            record["status"] = "FAILED"
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["unload_elapsed_seconds"] = self.unload_clock() - started

    def verify(self, profile, digest):
        items = self.residents()
        if len(items) != 1:
            raise IsolationError(f"expected exactly one resident model, observed {len(items)}")
        endpoint, item = items[0]
        if endpoint != profile.endpoint or profile.model not in (item.get("name"), item.get("model")):
            raise IsolationError("exact resident model/endpoint mismatch")
        if not digest or item.get("digest") != digest:
            raise IsolationError("resident digest missing or mismatched")
        if item.get("context_length") != profile.num_ctx:
            raise IsolationError("effective context length absent or mismatched")
        if (item.get("num_gpu") is not None and profile.num_gpu not in (None, -1)
                and item["num_gpu"] != profile.num_gpu):
            raise IsolationError("observed GPU layer setting mismatched")
        if profile.effective_placement:
            vram = item.get("size_vram")
            if type(vram) not in (int, float) or not math.isfinite(vram) or (profile.num_gpu > 0 and vram <= 0) or (profile.num_gpu == 0 and vram != 0):
                raise IsolationError("observed VRAM does not match selected placement")
        return {"model": item.get("model", item.get("name")), "digest": item.get("digest"),
                "num_ctx": item.get("context_length"), "num_gpu": item.get("num_gpu"),
                "size_vram": item.get("size_vram"), "quantization": item.get("details", {}).get("quantization_level")}

    def prompt(self, workload, units):
        self.serial += 1
        nonce = hashlib.sha256(str(self.serial).encode()).hexdigest()[:16]
        schema = json.dumps(agent_action_json_schema(), sort_keys=True) if workload.name == "swarm" else ""
        return (f"Sample {nonce}. Timing exercise. Continue writing detailed technical prose for at least 2000 words. "
                "Do not stop early. No tools are executed.\n" + schema + "\n" +
                ("The project has modules, tests, functions, documentation and requirements. " * units) +
                "\nBegin a long detailed explanation now.")

    def sample(self, profile, digest, workload, units, phase, repetition):
        prompt = self.prompt(workload, units)
        budget = 1 if phase == "calibration" else workload.output_tokens
        options = {"num_ctx": profile.num_ctx, "num_predict": budget, "temperature": 0, "seed": 0}
        if profile.num_gpu is not None:
            options["num_gpu"] = profile.num_gpu
        payload = {"model": profile.model, "messages": [{"role": "user", "content": prompt}],
                   "stream": False, "think": False, "keep_alive": -1, "options": options}
        result = {"phase": phase, "repetition": repetition, "status": "FAIL", "error": None,
                  "expected_model": profile.model, "endpoint": profile.endpoint, "model_identity": None,
                  "requested_options": options, "requested_think": False, "effective_runtime": None,
                  "target_input_tokens": workload.input_tokens, "target_output_tokens": budget,
                  "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "prompt_characters": len(prompt),
                  "prompt_eval_count": None, "eval_count": None, "prompt_eval_duration": None, "eval_duration": None,
                  "prompt_tokens_per_second": None, "generation_tokens_per_second": None, "wall_seconds": None}
        started = self.clock()
        try:
            response = self.transport.request(profile.endpoint, "/api/chat", payload)
        except Exception as exc:
            result.update(wall_seconds=self.clock()-started, error=f"{type(exc).__name__}: {exc}", status="TRANSPORT_ERROR")
            return result, True  # Timed-out generation may still be running; no next model.
        result["wall_seconds"] = self.clock() - started
        for key in ("prompt_eval_count", "eval_count", "prompt_eval_duration", "eval_duration", "load_duration", "total_duration", "done_reason"):
            value = response.get(key)
            result[key] = value if not isinstance(value, float) or math.isfinite(value) else None
        result["model_identity"] = response.get("model")
        try:
            result["effective_runtime"] = self.verify(profile, digest)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result, True
        if response.get("model") != profile.model or response.get("done") is not True:
            result["error"] = "response identity mismatch or incomplete generation"
            return result, True
        for prefix, rate_key in (("prompt", "prompt_tokens_per_second"), ("", "generation_tokens_per_second")):
            count_key = "prompt_eval_count" if prefix else "eval_count"
            duration_key = "prompt_eval_duration" if prefix else "eval_duration"
            n, ns = result[count_key], result[duration_key]
            if type(n) is int and n > 0 and finite_positive(ns):
                result[rate_key] = n * 1e9 / ns
        if phase != "calibration" and not workload.accepts(result["prompt_eval_count"]):
            result["error"] = "INPUT_SIZE_MISMATCH"
        elif type(result["eval_count"]) is not int or result["eval_count"] != budget:
            result["error"] = "OUTPUT_SIZE_MISMATCH (num_predict is only a cap)"
        elif any(result[key] is None for key in METRICS):
            result["error"] = "MISSING_OR_INVALID_METRICS"
        else:
            result["status"] = "PASS"
        return result, False

    def stats(self, runs):
        valid = [r for r in runs if r["phase"] == "warm" and r["status"] == "PASS"]
        return {metric: aggregate([r[metric] for r in valid]) for metric in STAT_METRICS}

    def workload(self, profile, digest, workload, entry):
        entry.update(workload=asdict(workload), calibration=[], runs=[], status="RUNNING", unstable=False)
        # Reserve enough headroom for output and chat framing at the upper input tolerance.
        if profile.num_ctx < workload.input_tokens * 1.15 + workload.output_tokens + 128:
            entry.update(status="UNSUPPORTED_CONTEXT", error="configured num_ctx insufficient for workload")
            return
        units = max(1, workload.input_tokens // 14)
        calibrated = False
        for attempt in range(3):
            self.empty()
            run, fatal = self.sample(profile, digest, workload, units, "calibration", attempt)
            entry["calibration"].append(run)
            self.save(self.report)
            if fatal:
                raise IsolationError(run["error"])
            measured = run["prompt_eval_count"]
            if type(measured) is not int or measured <= 0:
                break
            if workload.accepts(measured):
                calibrated = True
                break
            units = max(1, min(20000, round(units * workload.input_tokens / measured)))
        if not calibrated:
            entry.update(status="INPUT_CALIBRATION_FAILED")
            return
        self.empty()  # Calibration never warms the measured cold run.
        for repetition in range(6):
            if repetition:
                self.verify(profile, digest)
            run, fatal = self.sample(profile, digest, workload, units, "cold" if repetition == 0 else "warm", repetition)
            entry["runs"].append(run)
            self.save(self.report)
            if fatal:
                raise IsolationError(run["error"])
        entry["initial_warm_statistics"] = self.stats(entry["runs"])
        initial = entry["initial_warm_statistics"]["generation_tokens_per_second"]
        cv = initial["cv_percent"] if initial else None
        if cv is not None and cv > 10:
            entry["unstable"] = True  # Preserve the original signal even if five extra runs reduce CV.
            for repetition in range(6, 11):
                self.verify(profile, digest)
                run, fatal = self.sample(profile, digest, workload, units, "warm", repetition)
                entry["runs"].append(run)
                self.save(self.report)
                if fatal:
                    raise IsolationError(run["error"])
        entry["warm_statistics"] = self.stats(entry["runs"])
        entry["status"] = "PASS" if all(r["status"] == "PASS" for r in entry["runs"]) else "FAILED_SAMPLES"

    def rank(self):
        candidates = []
        for model in self.report["models"]:
            model["performance_summary"] = profile_summary(model)
            decode = next((w for w in model["workloads"] if w["workload"]["name"] == "decode"), None)
            if not decode or decode["status"] != "PASS":
                continue
            stats = decode["warm_statistics"]["generation_tokens_per_second"]
            if not stats or stats["count"] < 5 or stats["cv_percent"] is None:
                continue
            candidates.append({"profile_id": model["profile"]["profile_id"], "median_warm_generation_tokens_per_second": stats["median"],
                               "cv_percent": stats["cv_percent"], "warm_count": stats["count"], "unstable": decode["unstable"]})
        self.report["speed_ranking"] = sorted(candidates, key=lambda c: (-c["median_warm_generation_tokens_per_second"], c["profile_id"]))
        self.report["stability_ranking"] = sorted(candidates, key=lambda c: (c["cv_percent"], c["profile_id"]))

    def run(self):
        # Register every profile up front, so an isolation stop never silently omits later ones.
        self.report["models"] = [{"profile": asdict(p), "status": "NOT_RUN", "workloads": [], "quantization": None} for p in self.profiles]
        try:
            self.empty()
            for profile, model in zip(self.profiles, self.report["models"]):
                if not profile.model or "TODO" in profile.model:
                    model["status"] = "UNRESOLVED_MODEL_TAG"
                    continue
                tags = self.transport.request(profile.endpoint, "/api/tags").get("models", [])
                match = next((m for m in tags if profile.model in (m.get("name"), m.get("model"))), None)
                if not match or not match.get("digest"):
                    model["status"] = "EXACT_MODEL_NOT_AVAILABLE"
                    continue
                model["digest"] = match["digest"]
                details = self.transport.request(profile.endpoint, "/api/show", {"model": profile.model})
                model["quantization"] = details.get("details", {}).get("quantization_level")
                model["status"] = "RUNNING"
                for workload in WORKLOADS:
                    entry = {}
                    model["workloads"].append(entry)
                    self.workload(profile, match["digest"], workload, entry)
                    self.empty()
                model["status"] = "PASS" if all(w["status"] == "PASS" for w in model["workloads"]) else "INCOMPLETE"
                self.save(self.report)
        except Exception as exc:
            self.report["fatal_error"] = f"{type(exc).__name__}: {exc}"
            for model in self.report["models"]:
                if model["status"] == "RUNNING":
                    model["status"] = "ABORTED"
        finally:
            try:
                self.empty()
            except Exception as exc:
                self.report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            self.rank()
            self.save(self.report)
        return self.report


def profile_summary(model):
    result = {"decode": None, "swarm": None, "context_coverage": {}, "unsupported_workloads": []}
    for w in model["workloads"]:
        name = w["workload"]["name"]
        stats = w.get("warm_statistics", {}).get("generation_tokens_per_second")
        item = {"status": w["status"], "median_warm_generation_tokens_per_second": stats["median"] if stats else None,
                "cv_percent": stats["cv_percent"] if stats else None, "unstable": w["unstable"]}
        if name in ("decode", "swarm"):
            result[name] = item
        else:
            result["context_coverage"][name] = item
        if w["status"] == "UNSUPPORTED_CONTEXT":
            result["unsupported_workloads"].append(name)
    return result


def render(report):
    lines = ["# Local model performance (diagnostic only)", "", "Capability results are separate. These rankings do not determine Swarm suitability.",
             "", f"Fatal error: {report.get('fatal_error')}", f"Cleanup error: {report.get('cleanup_error')}", "",
             "| Profile | Workload | Status | Warm n | Median input tok/s | Median generation tok/s | Generation CV % | Instability |",
             "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for model in report["models"]:
        pid = model["profile"]["profile_id"]
        if not model["workloads"]:
            lines.append(f"| {pid} | N/A | {model['status']} | N/A | N/A | N/A | N/A | N/A |")
        for w in model["workloads"]:
            stats = w.get("warm_statistics", {})
            generation = stats.get("generation_tokens_per_second") or {}
            prompt = stats.get("prompt_tokens_per_second") or {}
            lines.append(f"| {pid} | {w['workload']['name']} | {w['status']} | {generation.get('count', 0)} | {prompt.get('median')} | {generation.get('median')} | {generation.get('cv_percent')} | {w['unstable']} |")
    lines += ["", "## Runtime profiles", ""]
    for model in report["models"]:
        p = model["profile"]
        observed = next((r.get("effective_runtime") for w in model["workloads"] for r in w.get("runs", []) if r.get("effective_runtime")), {})
        lines.append(f"- Production configuration for {p['profile_id']}: runtime={json.dumps(p.get('production_runtime'), sort_keys=True)}; context={json.dumps(p.get('production_context'), sort_keys=True)}; logical server={p.get('production_server')}.")
        if p.get("effective_placement"):
            lines.append("- Selected placement: " + json.dumps(p["effective_placement"], sort_keys=True))
        lines.append(f"- {p['profile_id']}: {p['model']}; {p['endpoint']}; digest={model.get('digest')}; observed size_vram={observed.get('size_vram')}; quantization={model['quantization']}; requested num_ctx={p['num_ctx']}; requested num_gpu={p['num_gpu']}; observed num_gpu={observed.get('num_gpu')}; observed context={observed.get('num_ctx')}; think=false (null means unavailable).")
        lines.append("  Performance and context coverage: " + json.dumps(profile_summary(model), sort_keys=True))
    lines += ["", "## Independent capability evidence", ""]
    capabilities = report.get("capability_results")
    if isinstance(capabilities, list):
        for capability in capabilities:
            lines.append(f"- {capability['model']} at {capability['endpoint']}: AgentLoop compatible={capability['agent_loop_compatible']}; source={capability['source']}. Not used by speed/stability ranking.")
    else:
        lines.append(str(capabilities))
    for key, title in (("speed_ranking", "Speed ranking: median warm decode throughput"), ("stability_ranking", "Stability ranking: warm decode CV (lower is better)")):
        lines += ["", "## " + title, ""]
        lines += [f"{i}. {r['profile_id']}: {r['median_warm_generation_tokens_per_second']:.3f} tok/s; CV {r['cv_percent']:.3f}%; unstable={r['unstable']}"
                  for i, r in enumerate(report.get(key, []), 1)] or ["No valid comparable decode workloads eligible."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--runtime-profile", choices=("production", "cpu-fallback"), default="production")
    parser.add_argument("--execution-profile", help="Exact Router V2 execution profile; optional")
    parser.add_argument("--profile-id", help="Exact configured profile ID; omitted means all profiles sequentially")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--capability-report", type=Path, action="append", default=[], help="Existing capability probe JSON, reported independently")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live required: benchmark unloads resident models and runs generations")
    if args.output.suffix.lower() != ".json":
        parser.error("--output must end in .json")
    from .config import ConfigLoader
    from .integration.single_endpoint import single_endpoint_models_document
    from .runtime_resources.gpu_coordination.lease import GpuHeavyLease
    from .runtime_resources.gpu_coordination.policy import GpuCoordinationPolicy
    from .model_runtime.policy import ModelLifecyclePolicy
    document = single_endpoint_models_document(ConfigLoader(args.root))
    isolation_endpoints = {s["ollama_base_url"] for s in document["servers"].values()}
    try:
        profiles = profiles_from_runtime(args.root, args.runtime_profile, args.profile_id, args.execution_profile)
    except ValueError as exc:
        parser.error(str(exc))
    if args.profile_id is not None:
        profiles = tuple(p for p in profiles if p.profile_id == args.profile_id)
        if not profiles:
            parser.error(f"unknown exact profile ID: {args.profile_id}")
    def save(report):
        report["live"] = True
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        args.output.with_suffix(".md").write_text(render(report), encoding="utf-8")
    benchmark = SequentialBenchmark(NativeTransport(args.timeout), profiles, save=save, isolation_endpoints=isolation_endpoints)
    if args.capability_report:
        capabilities = []
        for path in args.capability_report:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if (data.get("model"), data.get("endpoint")) not in {(p.model, p.endpoint) for p in profiles}:
                parser.error(f"capability report has no exact configured model/endpoint match: {path}")
            if type(data.get("agent_loop_compatible")) is not bool or not isinstance(data.get("checks"), list):
                parser.error(f"invalid capability report: {path}")
            capabilities.append({"source": str(path.resolve()), "model": data["model"], "endpoint": data["endpoint"],
                                 "agent_loop_compatible": data["agent_loop_compatible"], "checks": data["checks"],
                                 "effective_placement": data.get("effective_placement"),
                                 "configured_runtime": data.get("configured_runtime"),
                                 "observed_placement": data.get("observed_placement")})
        benchmark.report["capability_results"] = capabilities
    try:
        with ExitStack() as stack:
            for policy_type in (ModelLifecyclePolicy, GpuCoordinationPolicy):
                policy = policy_type.from_project_root(args.root)
                stack.enter_context(GpuHeavyLease(policy.lock_path()).held("model-performance-benchmark", timeout_seconds=5))
            report = benchmark.run()
    except Exception as exc:
        report = benchmark.report
        report["fatal_error"] = f"{type(exc).__name__}: {exc}"
        save(report)
    print(render(report))
    return 0 if not report.get("fatal_error") and not report.get("cleanup_error") and all(m["status"] == "PASS" for m in report["models"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
