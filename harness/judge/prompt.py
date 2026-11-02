'Bounded evidence-only prompt for judge Ornith Judge.'
from __future__ import annotations

from .errors import JudgeProtocolError
from .models import JudgeEvidenceBundle, JudgePolicy, canonical_json, JUDGE_ALLOWED_ACTIONS
from .protocol import judge_response_schema, judge_schema_fingerprint, judge_validation_feedback

SYSTEM_PROMPT = """You are the Phase 9 Ornith Final Judge in a coding swarm.
Phase 7 deterministic verification is immutable truth about its listed checks only. Phase 8 Ministral Critic is advisory diagnosis.
VERIFICATION_PASS is not SEMANTIC_ACCEPTANCE_PASS. PASSED does not require ACCEPT: public contract contradictions or unresolved mandatory obligations may require REPLAN.
Your job is to synthesize a FINAL WORKFLOW DECISION only: ACCEPT, RETRY, ESCALATE, REPLAN, BLOCKED, or HUMAN_REVIEW.
You MUST NOT change deterministic_outcome. A deterministically FAILED candidate can never be ACCEPTED.
You have no tools and may not modify files, execute commands, or claim that you executed the next action.
For evidence, copy only exact strings from allowed_evidence_ids. You must cite verification_report and critic_report.
Return only JSON matching response_contract; no markdown or commentary."""


class JudgePromptBuilder:
    def __init__(self, policy: JudgePolicy) -> None:
        self.policy = policy

    def prompts(self, bundle: JudgeEvidenceBundle, *, prior_error: str | None = None, prior_response: str | None = None) -> tuple[str, str]:
        payload = {
            "request_id": bundle.request_id,
            "objective": bundle.objective,
            "work_contract": dict(bundle.work_contract),
            "deterministic_verification": bundle.verification_report.to_dict(),
            "critic_report": bundle.critic_report.to_dict(),
            "candidate": {
                "head": bundle.candidate_head,
                "diff_sha256": bundle.candidate_diff_sha256,
                "dirty_paths": list(bundle.dirty_paths),
                "diff": bundle.candidate_diff_text,
            },
            "allowed_evidence_ids": sorted(bundle.allowed_evidence_ids),
            "response_contract": judge_response_schema(bundle),
            "allowed_decision_action_pairs": {d.value: sorted(a.value for a in actions) for d, actions in JUDGE_ALLOWED_ACTIONS.items()},
            "rules": {
                "deterministic_outcome_is_immutable": True,
                "accept_failed_candidate_is_forbidden": True,
                "critic_is_advisory_not_deterministic": True,
                "workspace_mutation_is_forbidden": True,
                "decision_is_not_executed_by_judge": True,
            },
        }
        payload['response_contract_fingerprint'] = judge_schema_fingerprint(payload['response_contract'])
        payload['verdict_fields'] = payload['response_contract']['required']
        payload['output_instruction'] = (
            'Supply every verdict_fields key with its exact contract type; do not add keys or invent defaults. '
            'A compact decision/next_action-only response is invalid. can_override_deterministic must be false. '
            'Use the allowed decision/action pairs. If evidence is insufficient, keep mandatory acceptance '
            'obligations UNRESOLVED and choose a supported non-ACCEPT disposition. '
            'A valid JSON object is not semantic acceptance and is not independent hidden acceptance.')
        if bundle.acceptance_obligations is not None:
            payload['frozen_acceptance_obligations'] = bundle.acceptance_obligations.to_dict()
            payload['public_witness_comparison'] = {
                'provenance':'MODEL_HYPOTHESIS', 'candidate_diff_sha256':bundle.candidate_diff_sha256,
                'generated_tests_and_critic_were_excluded':True,
                'evidence':[r.to_dict() for r in bundle.semantic_assessment],
                'observations':[r.to_dict() for r in bundle.public_witness_observations],
                'instruction':'Observed results are execution facts; expected results are frozen MODEL_HYPOTHESIS, not authority. '
                    'MATCH alone does not prove entailment. Validate expected results against original public requirements. '
                    'A disagreement or uncertainty must remain '
                    'UNRESOLVED/VIOLATED; downstream green tests cannot erase it. Use existing REPLAN for semantic revision.'}
            payload['post_mutation_evidence'] = {
                'candidate_diff':{'provenance':'CANDIDATE_DERIVED','content_ref':'candidate.diff'},
                'generated_test_diff':{'provenance':'GENERATED_TEST_DERIVED','content':bundle.generated_test_diff_text,
                    'independent_by_default':False},
                'explorer_hypotheses':{'provenance':'MODEL_HYPOTHESIS','content':list(bundle.explorer_hypotheses),
                    'authoritative':False}}
            payload['semantic_rules'] = {
                'verification_pass_is_not_semantic_acceptance':True,
                'mandatory_obligations_must_all_be_satisfied':True,
                'citation_validity_is_not_entailment':True,
                'model_hypothesis_is_not_authority':True,
                'implementation_is_not_normative_specification':True,
                'generated_tests_cannot_cancel_public_contract_contradictions':True,
                'instruction':'Evaluate each frozen obligation against the original public evidence and candidate diff. '
                    'A passing generated test is execution consistency only. State its expectation_basis using '
                    'independent original requirement/contract/test references, never candidate-derived assertions. '
                    'VIOLATED public contract or UNRESOLVED entailment cannot ACCEPT; choose existing REPLAN when '
                    'the task needs semantic revision. Do not infer intended behavior from a buggy original expression. '
                    'Docstrings/comments are public claims: assess applicability and conflicts against the objective. '
                    'public_interpretations were formed before candidate mutation WITHOUT implementation expressions '
                    'or explorer hypotheses. They are MODEL_HYPOTHESIS, not new specifications. Validate each against '
                    'its original public evidence, then compare the candidate behavior with that independent interpretation. '
                    'Do not replace its expected behavior with candidate-derived behavior simply because tests pass. '
                    'An unsupported interpretation stays UNRESOLVED. Every candidate_evidence must bind candidate_diff. '
                    'For a SATISFIED VERIFICATION obligation, expectation_basis cites its frozen verification '
                    'requirement; evidence_refs must be exactly ["verification_report"]. Normative requirements '
                    'and deterministic candidate support are separate. '
                    'Cite candidate_diff and relevant original evidence for each behavioral obligation. '
                    'The diff includes generated test changes (GENERATED_TEST_DERIVED), not an independent oracle.'}
        if prior_error is not None:
            from ..agent_loop.protocol import decode_strict_json
            try:
                rejected = decode_strict_json(prior_response or "")
                pair = {k: rejected.get(k) for k in ("decision", "next_action") if type(rejected.get(k)) is str and len(rejected[k]) <= 64}
            except (ValueError, TypeError, AttributeError):
                pair = {}
            payload["retry"] = {
                'validation': judge_validation_feedback(prior_response, bundle, prior_error),
                "rejected_pair": pair,
                "valid_pair_alternatives": payload["allowed_decision_action_pairs"],
                "protocol_error": prior_error.encode("utf-8")[:2048].decode("utf-8", "ignore"),
                "previous_response": "" if prior_response is None else prior_response.encode("utf-8")[: self.policy.max_response_bytes].decode("utf-8", "ignore"),
                "allowed_evidence_ids": sorted(bundle.allowed_evidence_ids),
                "instruction": "Produce a fresh complete response with every verdict_fields key and exact types from response_contract. Address validation diagnostics; never fabricate defaults or automatically map the rejected pair. Preserve deterministic truth and unresolved semantic evidence. Cite verification_report and critic_report exactly. Do not execute any action.",
            }
        user = "JUDGE_REQUEST\n" + canonical_json(payload)
        total = len(SYSTEM_PROMPT.encode("utf-8")) + len(user.encode("utf-8"))
        if total > self.policy.max_prompt_bytes:
            raise JudgeProtocolError(f"judge prompt exceeds max_prompt_bytes: {total}")
        return SYSTEM_PROMPT, user
