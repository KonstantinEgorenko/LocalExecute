'Outer research proposal model-call lineage for research diagnostics.5C.'
from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from ..research_campaign.model_proposals import ModelBackedResearchProposalGeneratorV1
from .mission_trace_observer import MissionTraceRegistry
from .model_invocation_lineage import (
    ModelInvocationKindV1,
    ModelInvocationManifestV1,
    ModelInvocationStatusV1,
    runtime_fingerprint,
    stable_fingerprint,
)

_CORRELATION_EVENT = "RESEARCH_PROPOSAL_LINEAGE_CORRELATED"


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _compact_diagnostic(error: Exception) -> str:
    """Content-minimized durable failure identity."""
    diagnostic_text = str(error)
    diagnostic_fingerprint = stable_fingerprint({
        "exception_type": type(error).__name__,
        "message": diagnostic_text,
    })
    return (
        f"{type(error).__name__}: "
        f"fingerprint={diagnostic_fingerprint}; "
        f"bytes={len(diagnostic_text.encode('utf-8'))}"
    )


def _normalized_output_fingerprint(content: str) -> str:
    try:
        return stable_fingerprint(json.loads(content))
    except (TypeError, json.JSONDecodeError):
        return stable_fingerprint(content)


def _runtime_metadata(runtime: Any, response: Any = None) -> tuple[str, str | None, str, str]:
    logical_model_id = (
        _text(getattr(response, "model_id", None))
        or _text(getattr(runtime, "model_id", None))
        or "research_proposal_model"
    )
    model_tag = _text(getattr(response, "model_tag", None))
    values: dict[str, Any] = {
        "runtime_kind": type(runtime).__name__,
        "logical_model_id": logical_model_id,
        "model_tag": model_tag,
        "context_length": getattr(response, "context_length", None),
        "output_tokens": getattr(runtime, "output_tokens", None),
        "timeout_seconds": getattr(runtime, "timeout_seconds", None),
    }
    registry = getattr(runtime, "registry", None)
    if registry is not None:
        try:
            definition = registry.get(logical_model_id)
            server = registry.get_server(definition.server)
            model_tag = model_tag or definition.ollama_model
            values.update({
                "model_tag": model_tag,
                "server_id": server.server_id,
                "execution": definition.runtime.get("execution"),
                "configured_num_gpu": definition.runtime.get("num_gpu"),
                "configured_context": definition.context.get("default"),
            })
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    return logical_model_id, model_tag, type(runtime).__name__, runtime_fingerprint(values)


class _ResearchProposalRuntimeProxy:
    def __init__(self, inner: Any, observer: "ResearchProposalInvocationObserverV1", *, spec: Any, packet: Any, generator_identity: str) -> None:
        self.inner = inner
        self.observer = observer
        self.spec = spec
        self.packet = packet
        self.generator_identity = generator_identity

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def complete(self, system_prompt: str, user_prompt: str, response_schema: Any = None) -> Any:
        invocation_id = f"{self.packet.campaign_id}:research-proposal:{uuid.uuid4().hex}"
        request_id = (
            f"{self.packet.campaign_id}:proposal:{self.packet.packet_id}:"
            f"{invocation_id.rsplit(':', 1)[-1][:12]}"
        )
        input_hash = stable_fingerprint({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_schema": response_schema,
        })
        started = time.monotonic()
        try:
            response = self.inner.complete(system_prompt, user_prompt, response_schema)
        except Exception as exc:
            logical_id, model_tag, runtime_kind, runtime_hash = _runtime_metadata(self.inner)
            self.observer._record_manifest(
                self.spec,
                ModelInvocationManifestV1(
                    schema_version=1,
                    invocation_id=invocation_id,
                    request_kind=ModelInvocationKindV1.RESEARCH_PROPOSAL,
                    mission_id=self.spec.mission_id,
                    goal_id=self.spec.goal_id,
                    campaign_id=self.packet.campaign_id,
                    request_id=request_id,
                    logical_model_id=logical_id,
                    model_tag=model_tag,
                    runtime_kind=runtime_kind,
                    runtime_fingerprint=runtime_hash,
                    evidence_packet_fingerprint=self.packet.fingerprint,
                    input_fingerprint=input_hash,
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    status=ModelInvocationStatusV1.FAILED,
                    diagnostic=_compact_diagnostic(exc),
                ),
            )
            raise

        content = str(getattr(response, "content", ""))
        logical_id, model_tag, runtime_kind, runtime_hash = _runtime_metadata(self.inner, response)
        context_length = _non_negative_int(getattr(response, "context_length", None))
        self.observer._record_manifest(
            self.spec,
            ModelInvocationManifestV1(
                schema_version=1,
                invocation_id=invocation_id,
                request_kind=ModelInvocationKindV1.RESEARCH_PROPOSAL,
                mission_id=self.spec.mission_id,
                goal_id=self.spec.goal_id,
                campaign_id=self.packet.campaign_id,
                request_id=request_id,
                logical_model_id=logical_id,
                model_tag=model_tag,
                runtime_kind=runtime_kind,
                runtime_fingerprint=runtime_hash,
                evidence_packet_fingerprint=self.packet.fingerprint,
                input_fingerprint=input_hash,
                output_fingerprint=_normalized_output_fingerprint(content),
                requested_context_length=context_length,
                observed_context_length=context_length,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                status=ModelInvocationStatusV1.SUCCESS,
            ),
        )
        return response


class ResearchProposalInvocationObserverV1:
    def __init__(self, state_root: Path, registry: MissionTraceRegistry | None = None) -> None:
        self.state_root = Path(state_root)
        self.registry = registry or MissionTraceRegistry(self.state_root)
        self.last_error: str | None = None
        self._physical_invocation_ids_this_process: list[str] = []
        self._correlations_this_process: list[str] = []

    def _recorder(self, spec: Any):
        recorder = self.registry.get(spec.mission_id, spec.goal_id)
        if hasattr(spec, "to_dict"):
            recorder.register_request_secrets(spec.to_dict())
        return recorder

    def _record_manifest(self, spec: Any, manifest: ModelInvocationManifestV1) -> None:
        try:
            self._recorder(spec).record_model_invocation(manifest)
        except Exception as exc:
            self.last_error = (
                "research proposal invocation trace write failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return
        self._physical_invocation_ids_this_process.append(manifest.invocation_id)

    def wrap_proposal_factory(self, *, spec: Any, generator_identity: str, proposal_factory: Callable[[Any], Any]) -> Callable[[Any], Any]:
        def traced_factory(packet: Any) -> Any:
            generator = proposal_factory(packet)
            if not isinstance(generator, ModelBackedResearchProposalGeneratorV1):
                return generator
            model = generator.model
            original_runtime_factory = getattr(model, "runtime_factory", None)
            if not callable(original_runtime_factory):
                self.last_error = (
                    "research proposal model port has no replaceable runtime_factory; "
                    "continuing without invocation tracing"
                )
                return generator

            def traced_runtime_factory(*args: Any, **kwargs: Any) -> Any:
                runtime = original_runtime_factory(*args, **kwargs)
                return _ResearchProposalRuntimeProxy(
                    runtime,
                    self,
                    spec=spec,
                    packet=packet,
                    generator_identity=generator_identity,
                )

            try:
                traced_model = replace(model, runtime_factory=traced_runtime_factory)
                return replace(generator, model=traced_model)
            except (TypeError, ValueError) as exc:
                self.last_error = (
                    "research proposal model port could not be decorated; "
                    f"continuing without invocation tracing: {type(exc).__name__}: {exc}"
                )
                return generator
        return traced_factory

    @staticmethod
    def _research_manifest_events(recorder: Any, campaign_id: str):
        result = []
        for event in recorder.events():
            if event.get("event_type") != "MODEL_INVOCATION_MANIFEST":
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            if payload.get("request_kind") != ModelInvocationKindV1.RESEARCH_PROPOSAL.value:
                continue
            if payload.get("campaign_id") != campaign_id:
                continue
            result.append((event, payload))
        return result

    @staticmethod
    def _correlation_events(recorder: Any, campaign_id: str):
        result = []
        for event in recorder.events():
            if event.get("event_type") != _CORRELATION_EVENT:
                continue
            payload = event.get("payload")
            if isinstance(payload, Mapping) and payload.get("campaign_id") == campaign_id:
                result.append((event, payload))
        return result

    @staticmethod
    def _checkpoint_for_cohort(cohort: Any, checkpoints: tuple[Any, ...]) -> Any | None:
        proposal_ids = {item.proposal_id for item in cohort.proposals}
        matches = [
            item for item in checkpoints
            if getattr(getattr(item, "selected_proposal", None), "proposal_id", None) in proposal_ids
        ]
        if not matches:
            return None
        matches.sort(
            key=lambda item: (
                getattr(item, "registered_experiment_id", None) is not None,
                str(getattr(item, "registered_experiment_id", "")),
                str(getattr(item, "decision_fingerprint", "")),
            ),
            reverse=True,
        )
        return matches[0]

    @staticmethod
    def _cohort_for_manifest(*, payload: Mapping[str, Any], event_sequence: int, cohorts: tuple[Any, ...], latest_sequence_by_evidence: Mapping[str, int]) -> Any | None:
        evidence_fp = payload.get("evidence_packet_fingerprint")
        candidates = tuple(
            item for item in cohorts
            if getattr(item, "evidence_packet_fingerprint", None) == evidence_fp
        )
        if not candidates:
            return None

        output_fp = payload.get("output_fingerprint")
        exact = tuple(
            item for item in candidates
            if getattr(item, "normalized_response_hash", None) is not None
            and getattr(item, "normalized_response_hash", None) == output_fp
        )
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None

        fallback = tuple(
            item for item in candidates
            if getattr(item, "normalized_response_hash", None) is None
            and bool(getattr(item, "fallback_used", False))
        )
        if (
            len(fallback) == 1
            and latest_sequence_by_evidence.get(str(evidence_fp)) == event_sequence
        ):
            return fallback[0]
        return None

    def finalize(self, *, spec: Any, bundle: Any) -> None:
        try:
            recorder = self._recorder(spec)
            campaign_id = spec.mission_id
            manifest_events = self._research_manifest_events(recorder, campaign_id)
            existing = {
                str(payload.get("invocation_id"))
                for _, payload in self._correlation_events(recorder, campaign_id)
            }
            cohorts = tuple(bundle.store.list_proposal_cohorts(campaign_id))
            checkpoints = tuple(bundle.store.list_strategy_checkpoints(campaign_id, limit=1000))

            latest_sequence_by_evidence: dict[str, int] = {}
            for event, payload in manifest_events:
                evidence_fp = payload.get("evidence_packet_fingerprint")
                if isinstance(evidence_fp, str):
                    latest_sequence_by_evidence[evidence_fp] = max(
                        int(event.get("sequence", 0)),
                        latest_sequence_by_evidence.get(evidence_fp, 0),
                    )

            for event, payload in manifest_events:
                invocation_id = payload.get("invocation_id")
                if not isinstance(invocation_id, str) or invocation_id in existing:
                    continue
                cohort = self._cohort_for_manifest(
                    payload=payload,
                    event_sequence=int(event.get("sequence", 0)),
                    cohorts=cohorts,
                    latest_sequence_by_evidence=latest_sequence_by_evidence,
                )
                if cohort is None:
                    continue

                checkpoint = self._checkpoint_for_cohort(cohort, checkpoints)
                selected_proposal_id = None if checkpoint is None else checkpoint.selected_proposal.proposal_id
                experiment_id = None
                decision_fingerprint = None
                planning_fingerprint = None
                if checkpoint is not None:
                    experiment_id = (
                        getattr(checkpoint, "registered_experiment_id", None)
                        or checkpoint.planned_experiment.experiment_id
                    )
                    decision_fingerprint = getattr(checkpoint, "decision_fingerprint", None)
                    planning_fingerprint = getattr(checkpoint, "planning_fingerprint", None)

                correlation_payload = {
                    "schema_version": 1,
                    "invocation_id": invocation_id,
                    "campaign_id": campaign_id,
                    "evidence_packet_fingerprint": cohort.evidence_packet_fingerprint,
                    "proposal_cohort_id": cohort.cohort_id,
                    "generator_identity": cohort.generator_identity,
                    "model_called": cohort.model_called,
                    "fallback_used": cohort.fallback_used,
                    "normalized_response_hash": cohort.normalized_response_hash,
                    "proposal_ids": [item.proposal_id for item in cohort.proposals],
                    "selected_proposal_id": selected_proposal_id,
                    "experiment_id": experiment_id,
                    "decision_fingerprint": decision_fingerprint,
                    "planning_fingerprint": planning_fingerprint,
                }
                recorder.record(
                    _CORRELATION_EVENT,
                    actor="research_strategy",
                    model_call_id=invocation_id,
                    payload=correlation_payload,
                )
                existing.add(invocation_id)
                self._correlations_this_process.append(invocation_id)
        except Exception as exc:
            self.last_error = (
                "research proposal lineage finalization failed: "
                f"{type(exc).__name__}: {exc}"
            )

    def summary(self, spec: Any) -> dict[str, Any]:
        try:
            recorder = self._recorder(spec)
            manifests = self._research_manifest_events(recorder, spec.mission_id)
            correlations = self._correlation_events(recorder, spec.mission_id)
            manifest_ids = {
                str(payload["invocation_id"])
                for _, payload in manifests
                if isinstance(payload.get("invocation_id"), str)
            }
            correlation_ids = {
                str(payload["invocation_id"])
                for _, payload in correlations
                if isinstance(payload.get("invocation_id"), str)
            }
            return {
                "schema_version": 1,
                "research_proposal_invocation_count": len(manifests),
                "research_proposal_correlation_count": len(correlations),
                "research_proposal_uncorrelated_count": len(manifest_ids - correlation_ids),
                "physical_invocation_count_this_process": len(self._physical_invocation_ids_this_process),
                "physical_invocation_ids_this_process": list(self._physical_invocation_ids_this_process),
                "correlation_count_this_process": len(self._correlations_this_process),
                "correlation_invocation_ids_this_process": list(self._correlations_this_process),
                "chains": [dict(payload) for _, payload in correlations],
                "last_error": self.last_error,
            }
        except Exception as exc:
            self.last_error = (
                "research proposal lineage summary failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return {
                "schema_version": 1,
                "research_proposal_invocation_count": 0,
                "research_proposal_correlation_count": 0,
                "research_proposal_uncorrelated_count": 0,
                "physical_invocation_count_this_process": len(self._physical_invocation_ids_this_process),
                "physical_invocation_ids_this_process": list(self._physical_invocation_ids_this_process),
                "correlation_count_this_process": len(self._correlations_this_process),
                "correlation_invocation_ids_this_process": list(self._correlations_this_process),
                "chains": [],
                "last_error": self.last_error,
            }


__all__ = ["ResearchProposalInvocationObserverV1"]
