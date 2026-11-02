'Evidence-only prompt construction for critic critic.'
from __future__ import annotations

from ..executor.models import ExecutionMessage
from .errors import CriticPolicyError
from .models import CriticEvidenceBundle, CriticPolicy, canonical_json
from .protocol import CRITIC_RESPONSE_SCHEMA
from .verification_context import verification_context

SYSTEM_PROMPT = """You are the Phase 8 Ministral Critic in a coding swarm.
You are advisory and evidence-bound. Phase 7 deterministic verification is the source of truth.
You MUST NOT change deterministic_outcome, claim a failed candidate is accepted, or invent evidence IDs.
For the evidence field, copy one or more strings EXACTLY from allowed_evidence_ids; never derive an ID from hashes, paths, report IDs, or other fields.
You have no tools and must not ask to modify files. Classify the result and recommend the next control action.
Use only evidence provided in the request. Return only JSON matching the supplied schema; no markdown."""
SYSTEM_PROMPT += """
Read authoritative_phase7 first. Copy its deterministic_outcome exactly.
regression_negative_controls test ORIGINAL_SOURCE, not the current candidate.
An expected assertion failure there can have gate_outcome PASSED despite raw_exit_code 1.
Raw process exit codes and pytest text do not override the authoritative gate outcome.
Unexpected process errors/timeouts are not successful negative controls.
You may report an independently grounded concern about the candidate while preserving
the authoritative deterministic_outcome. Passing Phase7 does not force ACCEPT.
"""


class CriticPromptBuilder:
    def __init__(self, policy: CriticPolicy) -> None:
        self.policy = policy

    def messages(self, bundle: CriticEvidenceBundle, *, prior_error: str | None = None, prior_response: str | None = None, alias_manifest=None) -> tuple[ExecutionMessage, ...]:
        context = verification_context(bundle)
        payload = {
            "request_id": bundle.request_id,
            "objective": bundle.objective,
            "work_contract": dict(bundle.work_contract),
            **context,
            "candidate": {
                "head": bundle.candidate_head,
                "diff_sha256": bundle.candidate_diff_sha256,
                "dirty_paths": list(bundle.dirty_paths),
                "diff": bundle.candidate_diff_text,
            },
            "allowed_evidence_ids": sorted(bundle.allowed_evidence_ids),
            "evidence_catalog": [item.to_dict() for item in bundle.evidence_items],
            "response_contract": CRITIC_RESPONSE_SCHEMA,
            "rules": {
                "deterministic_outcome_is_immutable": True,
                "accept_failed_candidate_is_forbidden": True,
                "cite_only_evidence_catalog_ids": True,
                "workspace_mutation_is_forbidden": True,
            },
        }
        system = SYSTEM_PROMPT
        if alias_manifest is not None:
            from copy import deepcopy
            from .evidence_aliases import alias_mode
            if not alias_mode(bundle):
                raise CriticPolicyError('alias prompt requires explicit contract')
            entries = alias_manifest.to_dict()['entries']
            mapping = {e['canonical_evidence_id']: e['alias'] for e in entries}
            payload.pop('allowed_evidence_ids')
            payload['allowed_evidence_aliases'] = list(alias_manifest.aliases)
            payload['evidence_manifest_fingerprint'] = alias_manifest.fingerprint
            payload['evidence_catalog'] = [{**{k:v for k,v in item.to_dict().items() if k not in ('evidence_id','reference')},
                'alias':mapping[item.evidence_id], 'content_sha256':next(e['content_sha256'] for e in entries if e['canonical_evidence_id']==item.evidence_id)} for item in bundle.evidence_items]
            payload['response_contract'] = deepcopy(CRITIC_RESPONSE_SCHEMA)
            payload['response_contract']['properties']['evidence']['items']['enum'] = list(alias_manifest.aliases)
            system = system.replace('allowed_evidence_ids','allowed_evidence_aliases').replace('invent evidence IDs','invent evidence aliases')
            system += '\nEvidence references MUST be invocation-local aliases E1, E2, etc. Never output canonical IDs. The harness resolves exact aliases against its sealed manifest; verdict and deterministic outcome are not repaired.'
        user = "CRITIC_REQUEST\n" + canonical_json(payload)
        messages: list[ExecutionMessage] = [ExecutionMessage("system", system), ExecutionMessage("user", user)]
        if prior_error is not None:
            retry = {
                "protocol_error": prior_error,
                "previous_response": "" if prior_response is None else prior_response[: self.policy.max_response_bytes],
                "allowed_evidence_ids": sorted(bundle.allowed_evidence_ids),
                "instruction": "Correct the response. For evidence, copy one or more strings verbatim from allowed_evidence_ids. Do not construct, extend, shorten, hash, or otherwise modify an evidence ID. Return only a schema-valid JSON object grounded in the same evidence.",
            }
            messages.append(ExecutionMessage("user", "CRITIC_RETRY\n" + canonical_json(retry)))
        total = sum(len(item.content.encode("utf-8")) for item in messages)
        if total > self.policy.max_prompt_bytes:
            raise CriticPolicyError(f"critic prompt exceeds max_prompt_bytes: {total}")
        return tuple(messages)
