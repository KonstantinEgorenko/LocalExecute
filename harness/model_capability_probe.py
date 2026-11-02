"""Opt-in, bounded diagnostics over the production Ollama transport; no mission state."""
from __future__ import annotations

import argparse
import json
import math
import re
import secrets
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from .diagnostic_runtime_profiles import diagnostic_models_document
from .agent_loop.errors import AgentProtocolError
from .agent_loop.protocol import agent_action_json_schema, decode_strict_json as strict_json, parse_agent_action
from .agent_loop import prompt as agent_prompt
from .agent_loop.prompt import tool_result_message
from .executor.client import OllamaTimeouts
from .integration.structured_worker_client import ProductionStructuredWorkerClient
from .workspace.models import ToolName, ToolResult, ToolStatus


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"


RAW_ACTION_LIMIT = 4096
OBSERVED_VALUE_LIMIT = 2048


def bounded_text(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8", errors="replace")
    return encoded[:limit].decode("utf-8", errors="ignore"), len(encoded) > limit


def bounded_value(value):
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False)
    preview, truncated = bounded_text(encoded, OBSERVED_VALUE_LIMIT)
    return ({"diagnostic_preview": preview} if truncated else value), truncated


@dataclass(frozen=True)
class ToolFailureEvidence:
    turn_index: int  # Zero-based, including the final continuation turn.
    expected_action: str
    expected_tool: str | None
    expected_arguments: dict
    observed_action: Any
    observed_tool: Any
    observed_arguments: Any
    observed_values_truncated: bool
    raw_agent_action: str
    raw_truncated: bool


class ToolCycleFailure(ValueError):
    def __init__(self, reason: str, evidence: ToolFailureEvidence):
        super().__init__(reason)
        self.evidence = evidence


def tool_failure_evidence(raw, turn_index, path):
    # Read-only inspection of the original JSON, never used to accept or execute it.
    try:
        value = strict_json(raw)
    except (ValueError, TypeError, RecursionError):
        value = None
    value = value if isinstance(value, dict) else {}
    call = value.get("tool_call")
    call = call if isinstance(call, dict) else {}
    observed = [bounded_value(v) for v in (value.get("action"), call.get("tool"), call.get("arguments"))]
    preview, truncated = bounded_text(raw, RAW_ACTION_LIMIT)
    return ToolFailureEvidence(turn_index, "TOOL" if path is not None else "FINAL",
        "READ_TEXT" if path is not None else None, {"path": path} if path is not None else {},
        *(v[0] for v in observed), any(v[1] for v in observed), preview, truncated)


@dataclass(frozen=True)
class Check:
    name: str
    required: bool
    status: Status
    detail: str
    evidence: ToolFailureEvidence | None = None


@dataclass(frozen=True)
class Measurement:
    check: str
    wall_seconds: float
    ttft_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    input_tokens_per_second: float | None
    output_tokens_per_second: float | None
    raw_tool_markers: tuple[str, ...]


@dataclass(frozen=True)
class ProbeReport:
    endpoint: str
    model: str
    num_ctx: int
    checks: tuple[Check, ...]
    measurements: tuple[Measurement, ...]
    schema_version: int = 1
    configured_runtime: dict | None = None
    effective_placement: dict | None = None
    observed_placement: dict | None = None

    @property
    def agent_loop_compatible(self) -> bool:
        required = [c for c in self.checks if c.required]
        return bool(required) and all(c.status is Status.PASS for c in required)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "agent_loop_compatible": self.agent_loop_compatible}

    def render(self) -> str:
        lines = [f"Model capability probe: {self.model}", f"Endpoint: {self.endpoint}; requested context: {self.num_ctx}",
                 f"AgentLoop compatible: {self.agent_loop_compatible}"]
        lines.append("Configured runtime: " + json.dumps(self.configured_runtime))
        lines.append("Selected placement: " + json.dumps(self.effective_placement))
        lines.append("Observed placement: " + json.dumps(self.observed_placement))
        for c in self.checks:
            lines.append(f"{c.status.value}: {c.name}{' [required]' if c.required else ''} - {c.detail}")
            if c.evidence is not None:
                # JSON escaping plus indentation keeps model text inert in Markdown.
                lines.append("    " + json.dumps(asdict(c.evidence), ensure_ascii=True, allow_nan=False))
        lines += ["TTFT unavailable for buffered production transport; token rates use server durations, not wall time."]
        for m in self.measurements:
            lines.append(f"  {m.check}: input={m.input_tokens_per_second} tok/s; output={m.output_tokens_per_second} tok/s; wall={m.wall_seconds:.3f}s")
        return "\n".join(lines) + "\n"


class Transport(Protocol):
    def request(self, path: str, payload: dict | None = None) -> dict: ...


class ProductionProbeTransport:
    """Reuse production payload overlay and HTTP/error handling, without runtime bootstrap."""
    def __init__(self, endpoint: str, timeout: float = 120):
        self.endpoint = endpoint
        self.client = ProductionStructuredWorkerClient(OllamaTimeouts(5, timeout, timeout))

    def request(self, path: str, payload: dict | None = None) -> dict:
        timeout = self.client.timeouts.discovery_timeout_seconds if payload is None else self.client.timeouts.generation_timeout_seconds
        return self.client._request(self.endpoint, path, timeout_seconds=timeout, payload=payload)


class Unavailable(ValueError):
    pass


def native_arguments(value: Any) -> dict:
    """Decode OpenAI JSON-string arguments only; never coerce scalar argument types."""
    parsed = strict_json(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be an object or JSON-encoded object")
    return parsed


def raw_tool_markers(content: str) -> tuple[str, ...]:
    patterns = (r"<tool_call>", r"\[TOOL_CALLS\]", r"<\|python_tag\|>", r'"tool_calls"\s*:', r'"name"\s*:\s*"READ_TEXT"')
    return tuple(p for p in patterns if re.search(p, content))


def positive_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def count(value):
    return value if type(value) is int and value >= 0 else None


def rate(tokens, duration):
    return tokens * 1e9 / duration if tokens is not None and positive_number(duration) else None


class CapabilityProbe:
    def __init__(self, transport: Transport, endpoint: str, model: str, *, num_ctx=8192, num_gpu=0,
                 long_chars=16000, max_tool_turns=2, nonce_factory=lambda: secrets.token_hex(8)):
        if type(num_ctx) is not int or not 1024 <= num_ctx <= 131072:
            raise ValueError("num_ctx must be an integer in [1024, 131072]")
        if type(long_chars) is not int or not 1024 <= long_chars <= 131072:
            raise ValueError("long_chars must be an integer in [1024, 131072]")
        if type(max_tool_turns) is not int or not 2 <= max_tool_turns <= 4:
            raise ValueError("max_tool_turns must be in [2, 4]")
        self.transport, self.endpoint, self.model = transport, endpoint, model
        self.num_ctx, self.num_gpu = num_ctx, num_gpu
        self.long_chars, self.max_tool_turns = long_chars, max_tool_turns
        self.nonce_factory = nonce_factory
        self.checks: list[Check] = []
        self.measurements: list[Measurement] = []

    def check(self, name, fn, *, required=True):
        try:
            detail = fn()
            result = Check(name, required, Status.PASS, detail or "verified")
        except ToolCycleFailure as exc:
            result = Check(name, required, Status.FAIL, str(exc), exc.evidence)
        except Unavailable as exc:
            result = Check(name, required, Status.UNAVAILABLE, str(exc))
        except Exception as exc:
            result = Check(name, required, Status.FAIL, f"{type(exc).__name__}: {exc}")
        self.checks.append(result)

    def chat(self, name, messages, schema=None, tools=None):
        payload = {"model": self.model, "stream": False, "messages": messages, "keep_alive": "5m",
                   "options": {"num_ctx": self.num_ctx, "num_gpu": self.num_gpu, "temperature": 0, "num_predict": 1024}}
        if schema is not None:
            payload["format"] = schema
        if tools is not None:
            payload["tools"] = tools
        start = time.perf_counter()
        response = self.transport.request("/api/chat", payload)
        elapsed = time.perf_counter() - start
        message = response.get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else ""
        self._last_response_content = content if isinstance(content, str) else ""
        inp, out = count(response.get("prompt_eval_count")), count(response.get("eval_count"))
        ttft = response.get("ttft_seconds")
        self.measurements.append(Measurement(name, elapsed, ttft if positive_number(ttft) else None, inp, out,
            rate(inp, response.get("prompt_eval_duration")), rate(out, response.get("eval_duration")),
            raw_tool_markers(content) if isinstance(content, str) else ()))
        if response.get("model") != self.model:
            raise ValueError(f"response model identity mismatch: {response.get('model')!r}")
        if response.get("done") is not True or response.get("done_reason") in ("length", "max_tokens"):
            raise ValueError("incomplete/truncated generation")
        if not isinstance(message, dict) or not isinstance(content, str):
            raise ValueError("malformed chat message")
        return response

    def text(self, name, prompt, schema=None):
        response = self.chat(name, [{"role": "user", "content": prompt}], schema)
        return response["message"]["content"]

    def identity(self):
        data = self.transport.request("/api/ps")
        models = data.get("models")
        if not isinstance(models, list):
            raise ValueError("/api/ps missing models list")
        matches = [m for m in models if isinstance(m, dict) and self.model in (m.get("name"), m.get("model"))]
        if not matches:
            raise Unavailable("exact requested model not reported as loaded; no fuzzy matching")
        return json.dumps(matches, ensure_ascii=False)

    def plain(self):
        if self.text("plain_chat", "Reply exactly PROBE_OK.").strip() != "PROBE_OK":
            raise ValueError("unexpected plain answer")

    def structured(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["probe", "value"],
                  "properties": {"probe": {"type": "string", "enum": ["ok"]}, "value": {"type": "integer", "enum": [7]}}}
        value = strict_json(self.text("strict_json", 'Return exactly {"probe":"ok","value":7}.', schema))
        if value != {"probe": "ok", "value": 7} or type(value["value"]) is not int:
            raise ValueError("strict JSON contract mismatch")

    def action(self, raw):
        return parse_agent_action(raw)

    def schema(self):
        action = self.action(self.text("agent_action", 'Return a FINAL AgentAction with summary PROBE_OK. '
                            + json.dumps(agent_action_json_schema()), agent_action_json_schema()))
        if action.final is None or action.final.summary != "PROBE_OK":
            raise ValueError("unexpected AgentAction")

    @staticmethod
    def read_arguments(arguments, expected):
        if set(arguments) != {"path"} or type(arguments["path"]) is not str or arguments["path"] != expected:
            raise ValueError("READ_TEXT requires exact string path; no scalar coercion")

    def cycle_action(self, name, messages, turn_index, path, expected_summary=None):
        self._last_response_content = ""
        raw = ""
        try:
            raw = self.chat(name, messages, agent_action_json_schema())["message"]["content"]
            action = self.action(raw)
            if path is not None:
                if action.tool_call is None or action.tool_call.tool is not ToolName.READ_TEXT:
                    raise ValueError("expected READ_TEXT tool action before FINAL")
                self.read_arguments(action.tool_call.arguments, path)
            elif action.final is None or action.final.summary != expected_summary:
                raise ValueError("continuation did not use tool results or exceeded tool budget")
            return raw
        except Exception as exc:
            evidence = tool_failure_evidence(raw or self._last_response_content, turn_index, path)
            raise ToolCycleFailure(str(exc), evidence) from exc

    def cycle(self, turns):
        name = "tool_cycle" if turns == 1 else "bounded_multi_turn"
        paths = [f"step-{i}.txt" for i in range(turns)]
        messages = [{"role": "system", "content": agent_prompt._SYSTEM},
                    {"role": "user", "content": "Use canonical AgentAction JSON. READ_TEXT these virtual files in order: "
                     + json.dumps(paths) + ". Then FINAL summary must contain their contents joined by commas. "
                     + json.dumps(agent_action_json_schema())}]
        values = []
        for i, path in enumerate(paths):
            raw = self.cycle_action(name, messages, i, path)
            value = self.nonce_factory()  # Not revealed until after the tool call.
            values.append(value)
            result = ToolResult(f"probe-{i}", "probe", "probe", ToolName.READ_TEXT, ToolStatus.SUCCESS, value)
            messages += [{"role": "assistant", "content": raw}, tool_result_message(result).to_dict()]
        self.cycle_action(name, messages, turns, None, ",".join(values))
        return f"{turns} virtual read(s), then correct FINAL; no filesystem tools executed"

    def native(self):
        tools = [{"type": "function", "function": {"name": "READ_TEXT", "description": "Read a virtual file",
                 "parameters": {"type": "object", "required": ["path"], "additionalProperties": False,
                                "properties": {"path": {"type": "string"}}}}}]
        messages = [{"role": "user", "content": "Call READ_TEXT with path step-0.txt. Then reply with only the tool result."}]
        response = self.chat("native_tool_calls", messages, tools=tools)
        message = response["message"]
        calls = message.get("tool_calls")
        if not calls:
            raise Unavailable("no native tool_calls; raw text is diagnostic only")
        if not isinstance(calls, list) or len(calls) != 1:
            raise ValueError("expected one native call")
        fn = calls[0]["function"]
        if fn["name"] != "READ_TEXT":
            raise ValueError("unexpected native tool")
        self.read_arguments(native_arguments(fn["arguments"]), "step-0.txt")
        value = self.nonce_factory()
        feedback = {"role": "tool", "tool_name": "READ_TEXT", "content": value}
        if "id" in calls[0]:
            feedback["tool_call_id"] = calls[0]["id"]
        continued = self.chat("native_continuation", messages + [message, feedback], tools=tools)
        if continued["message"].get("tool_calls") or continued["message"]["content"].strip() != value:
            raise ValueError("native continuation failed")
        return "native object/OpenAI JSON-string arguments parsed; continuation verified"

    def raw(self):
        content = self.text("raw_tool_output", 'Emit your raw tool-call syntax for READ_TEXT with path step-0.txt; do not execute it.')
        markers = raw_tool_markers(content)
        if not markers:
            raise Unavailable("no recognized raw tool-call markers; this does not imply native support")
        return "observed markers only, never executed: " + ", ".join(markers)

    def arguments(self):
        for raw in ({"path": "step-0.txt"}, '{"path":"step-0.txt"}'):
            self.read_arguments(native_arguments(raw), "step-0.txt")
        for raw in ('[]', '{"path":1}', '{"path":true}', '{"path":'):
            try:
                self.read_arguments(native_arguments(raw), "step-0.txt")
            except ValueError:
                continue
            raise ValueError("unsafe argument coercion accepted")
        return "local parser control: object/JSON-string decoding only; scalar coercion rejected; model arguments checked in cycles"

    def malformed(self):
        for raw in ('{', '```json\n{}\n```', '{}', '{"schema_version":1,"action":"TOOL","tool_call":null,"final":null}'):
            try:
                parse_agent_action(raw)
            except AgentProtocolError:
                continue
            raise ValueError("production parser accepted malformed fixture")
        return "local production parser controls reject malformed/truncated fixtures; every live response also checked for done/length"

    def context(self):
        first, last = self.nonce_factory(), self.nonce_factory()
        prompt = f"FIRST={first}\n" + ("unrelated filler text. " * (self.long_chars // 23)) + f"\nLAST={last}\nReturn FIRST,LAST values only, comma-separated."
        response = self.chat("long_context", [{"role": "user", "content": prompt}])
        if response["message"]["content"].strip() != first + "," + last:
            raise ValueError("long-context sentinel retrieval failed")
        tokens = count(response.get("prompt_eval_count"))
        if tokens is None:
            raise Unavailable("sentinels recovered but server omitted input token count; context coverage unverified")
        if not 128 <= tokens < self.num_ctx:
            raise ValueError(f"input token count {tokens} outside sanity range [128, {self.num_ctx})")
        return f"both sentinels recovered from {len(prompt)} characters / {tokens} input tokens; basic sanity only, not proof of maximum context"

    def run(self):
        self.checks.clear()
        self.measurements.clear()
        self.check("reachability", lambda: str(self.transport.request("/api/version")["version"]))
        reachable = self.checks[-1].status is Status.PASS
        checks = [("loaded_identity", self.identity, False), ("plain_chat", self.plain, True),
                  ("strict_json", self.structured, True), ("agent_action", self.schema, True),
                  ("tool_cycle", lambda: self.cycle(1), True),
                  ("bounded_multi_turn", lambda: self.cycle(self.max_tool_turns), True),
                  ("native_tool_calls", self.native, False), ("raw_tool_output", self.raw, False),
                  ("long_context", self.context, True)]
        for name, fn, required in checks:
            if reachable:
                self.check(name, fn, required=required)
            else:
                self.checks.append(Check(name, required, Status.UNAVAILABLE, "endpoint unreachable; not attempted"))
        self.check("argument_parsing", self.arguments)
        self.check("malformed_detection", self.malformed)
        return ProbeReport(self.endpoint, self.model, self.num_ctx, tuple(self.checks), tuple(self.measurements))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly authorize bounded local model generations")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--runtime-profile", choices=("production", "cpu-fallback"), default="production")
    parser.add_argument("--execution-profile", help="Exact Router V2 execution profile")
    parser.add_argument("--num-ctx", type=int, default=None, help="Explicit diagnostic context override")
    parser.add_argument("--long-chars", type=int, default=16000)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", type=Path, required=True, help="JSON output; companion .md report is also written")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("network/model calls require --live")
    if args.output.suffix.lower() != ".json":
        parser.error("--output must end in .json (a separate .md report is written)")
    from .config import ConfigLoader
    from .integration.single_endpoint import single_endpoint_models_document
    from .runtime_resources.gpu_coordination.lease import GpuHeavyLease
    from .runtime_resources.gpu_coordination.policy import GpuCoordinationPolicy
    from .model_runtime.policy import ModelLifecyclePolicy
    try:
        document = diagnostic_models_document(args.root, args.runtime_profile,
            args.model_id or (None if args.execution_profile else ConfigLoader(args.root).load_lead()["lead"]["model_id"]), args.execution_profile)
    except ValueError as exc:
        parser.error(str(exc))
    model = next(iter(document["models"].values()))
    selected_context = model["context"]["default"]
    args.num_ctx = selected_context if args.num_ctx is None else args.num_ctx
    endpoint = document["servers"][model["server"]]["ollama_base_url"]
    if urlparse(endpoint).hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("probe supports configured loopback endpoints only")
    if "TODO" in model["ollama_model"]:
        parser.error("exact configured model tag is unresolved")
    if args.num_ctx > model["context"]["operational_max"]:
        parser.error("requested context exceeds configured operational maximum")
    transport = ProductionProbeTransport(endpoint, args.timeout)
    probe = CapabilityProbe(transport, endpoint, model["ollama_model"], num_ctx=args.num_ctx,
                            num_gpu=model["runtime"]["num_gpu"], long_chars=args.long_chars)
    # Same authoritative leases and order as production; no runtime controller/bootstrap.
    try:
        with ExitStack() as stack:
            for policy_type in (ModelLifecyclePolicy, GpuCoordinationPolicy):
                policy = policy_type.from_project_root(args.root)
                stack.enter_context(GpuHeavyLease(policy.lock_path()).held("model-capability-probe", timeout_seconds=5))
            report = probe.run()
            observed = None
            try:
                data = transport.request("/api/ps")
                residents = data["models"]
                if not isinstance(residents, list) or len(residents) != 1:
                    raise ValueError("expected exactly one resident for selected placement")
                observed = residents[0]
                if observed.get("name", observed.get("model")) != model["ollama_model"]:
                    raise ValueError("selected placement model mismatch")
                layers = model["runtime"]["num_gpu"]
                vram = observed.get("size_vram")
                if (type(vram) not in (int, float) or not math.isfinite(vram) or
                        (layers > 0 and vram <= 0) or (layers == 0 and vram != 0) or
                        observed.get("context_length") != args.num_ctx or
                        ("num_gpu" in observed and observed["num_gpu"] != layers)):
                    raise ValueError("observed residency does not match selected placement")
                placement_check = Check("runtime_placement", True, Status.PASS, "selected context and residency verified; layers checked when reported")
            except Exception as exc:
                placement_check = Check("runtime_placement", True, Status.FAIL, f"{type(exc).__name__}: {exc}")
            report = replace(report, checks=report.checks + (placement_check,), observed_placement=observed)
    except Exception as exc:
        # Lock failure must not trigger unguarded generation or a success report.
        report = ProbeReport(endpoint, model["ollama_model"], args.num_ctx,
                             (Check("live_guard", True, Status.FAIL, f"{type(exc).__name__}: {exc}"),), ())
    placement = dict(model["effective_placement"])
    placement["requested_num_ctx"] = args.num_ctx
    placement["context_override"] = args.num_ctx != selected_context
    report = replace(report, configured_runtime=model["configured_runtime"], effective_placement=placement)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(report.render(), encoding="utf-8")
    print(report.render())
    return 0 if report.agent_loop_compatible else 1


if __name__ == "__main__":
    raise SystemExit(main())
