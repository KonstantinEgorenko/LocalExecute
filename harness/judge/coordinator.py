'judge bounded Ornith Judge coordination.'
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from dataclasses import replace

from ..lead.runtime import LeadModelError
from ..protocol_transport import ollama_compatible_schema
from ..worktree.manager import GitWorktreeManager
from .errors import JudgeExhaustedError, JudgeIntegrityError, JudgeProtocolError
from .evidence import JudgeEvidenceResolver
from .models import JudgeCoordinatorResult, JudgePolicy, JudgeReport, JudgeTurnRecord, JudgeTurnStatus
from .prompt import JudgePromptBuilder
from .protocol import parse_judge_response
from .publisher import JudgePublisher


class JudgeCoordinator:
    def __init__(self, *, run_store: Any, artifact_store: Any, manager: GitWorktreeManager, runtime: Any, policy: JudgePolicy, require_acceptance=False, public_witness_port=None) -> None:
        self.run_store = run_store
        self.artifact_store = artifact_store
        self.manager = manager
        self.runtime = runtime
        self.require_acceptance = require_acceptance
        self.public_witness_port = public_witness_port
        self.policy = policy
        self.resolver = JudgeEvidenceResolver(run_store=run_store, artifact_store=artifact_store, manager=manager, policy=policy, require_acceptance=require_acceptance)
        self.publisher = JudgePublisher(run_store=run_store, artifact_store=artifact_store)
        self.prompts = JudgePromptBuilder(policy)

    def interpret_public_requirements(self, frozen):
        """Candidate-free interpretation with existing bounded, durable repair."""
        from ..verification.acceptance import public_interpretation_request, public_witness_repair_request, fingerprint, canonical, AcceptanceObligationsV1
        from ..verification.witness_domain import validate_witness_inputs
        from ..agent_loop.protocol import decode_strict_json
        from ..context_manager.models import Artifact, ArtifactKind, ArtifactOrigin
        from .protocol import parse_public_interpretations
        request_id=f'{frozen.run_id}:public-interpretation:{fingerprint(frozen.to_dict())[:20]}'
        system,user,schema=public_interpretation_request(frozen)
        if len(system.encode('utf-8'))+len(user.encode('utf-8')) > self.policy.max_prompt_bytes:
            return replace(frozen,unresolved_semantics=(*frozen.unresolved_semantics,'Public interpretation context exceeds bound'))
        feedback=None
        for attempt in range(min(2,self.policy.max_model_attempts)):
            turn=self.publisher.find_turn(request_id,attempt)
            if turn is None:
                call_system,call_user,call_schema=(system,user,schema) if feedback is None else feedback
                if len((call_system+call_user).encode('utf-8'))>self.policy.max_prompt_bytes:
                    break
                if feedback is not None:
                    context_id=f'{request_id}:repair-context:{attempt}'
                    context={'system':call_system,'user':call_user,'response_contract':call_schema}
                    context_identity={'snapshot':fingerprint(frozen.to_dict()),
                        'proposal':rejected_proposal_fingerprint,'attempt':attempt,'context':fingerprint(context)}
                    if self.artifact_store.has_artifact(context_id):
                        saved=self.artifact_store.get_artifact(context_id)
                        if saved.source!='PublicWitnessRepairContext' or decode_strict_json(saved.content)!={'identity':context_identity,'context':context}:
                            raise JudgeIntegrityError('witness repair context identity mismatch')
                    else:
                        self.artifact_store.create_artifact(Artifact.from_text(artifact_id=context_id,
                            kind=ArtifactKind.OTHER,origin=ArtifactOrigin.CONTROL_PLANE,
                            content=canonical({'identity':context_identity,'context':context}),media_type='application/json',
                            source='PublicWitnessRepairContext',created_at=datetime.now(timezone.utc).isoformat(),metadata=context_identity))
                try:
                    response=self.runtime.complete(call_system,call_user,ollama_compatible_schema(call_schema))
                    if response.model_id != self.policy.model_id or type(response.content) is not str or len(response.content.encode('utf-8')) > self.policy.max_response_bytes:
                        raise JudgeProtocolError('invalid public interpretation model/response')
                    turn=JudgeTurnRecord(request_id,attempt,JudgeTurnStatus.RESPONSE,response.model_id,response.model_tag,response.content,None)
                except (LeadModelError,JudgeProtocolError) as exc:
                    turn=JudgeTurnRecord(request_id,attempt,JudgeTurnStatus.EXECUTION_ERROR,self.policy.model_id,None,None,str(exc)[:2048])
                self.publisher.publish_turn(turn)
            if turn.status is JudgeTurnStatus.EXECUTION_ERROR:
                return replace(frozen,unresolved_semantics=(*frozen.unresolved_semantics,'Public interpretation transport unavailable'))
            try:
                interpretations=parse_public_interpretations(turn.content,frozen)
            except (ValueError,TypeError) as exc:
                return replace(frozen,unresolved_semantics=(*frozen.unresolved_semantics,'Public interpretation invalid: '+str(exc)[:512]))
            artifact_id=f'{request_id}:input-domain:{attempt}'
            identity={'snapshot':fingerprint(frozen.to_dict()),'proposal':fingerprint(turn.content),'attempt':attempt}
            if self.artifact_store.has_artifact(artifact_id):
                artifact=self.artifact_store.get_artifact(artifact_id)
                raw=decode_strict_json(artifact.content)
                if artifact.source!='PublicWitnessInputDomain' or raw['identity']!=identity:
                    raise JudgeIntegrityError('witness-domain checkpoint identity mismatch')
                checked=AcceptanceObligationsV1.from_dict(raw['checked'])
                if replace(checked,public_interpretations=())!=replace(frozen,public_interpretations=()):
                    raise JudgeIntegrityError('witness-domain checkpoint changed public snapshot')
            else:
                runner=getattr(self.public_witness_port,'runner',None)
                interpretations=validate_witness_inputs(frozen,interpretations,runner,decode=decode_strict_json)
                checked=replace(frozen,public_interpretations=interpretations)
                self.artifact_store.create_artifact(Artifact.from_text(artifact_id=artifact_id,
                    kind=ArtifactKind.OTHER,origin=ArtifactOrigin.CONTROL_PLANE,
                    content=canonical({'identity':identity,'checked':checked.to_dict()}),media_type='application/json',
                    source='PublicWitnessInputDomain',created_at=datetime.now(timezone.utc).isoformat(),metadata=identity))
            rejected=[i.input_validation for i in checked.public_interpretations if i.input_validation is not None and i.input_validation.status!='VALID']
            if not rejected:
                return checked
            # Applicability/unsupported execution cannot be repaired by inventing
            # units. Only a definitively rejected original call gets one repair.
            if any(r.status!='INVALID' for r in rejected):
                return checked
            if attempt == 0:
                rejected_proposal_fingerprint=fingerprint(turn.content)
                feedback=public_witness_repair_request(frozen,checked,
                    proposal_fingerprint=rejected_proposal_fingerprint,attempt_index=1)
        return checked if 'checked' in locals() else replace(frozen,unresolved_semantics=(*frozen.unresolved_semantics,'Public interpretation repair unavailable'))

    def compare_public_contract(self, bundle, workspace=None):
        """Counterexamples block acceptance; a witness match is not proof."""
        from ..agent_loop.protocol import decode_strict_json
        from ..verification.acceptance import AcceptanceEvidenceV1, fingerprint, canonical
        from ..verification.public_witness import PublicWitnessObservationV1
        from ..context_manager.models import Artifact, ArtifactKind, ArtifactOrigin
        frozen = bundle.acceptance_obligations
        if frozen is None or not self.require_acceptance:
            return bundle, 0
        identity = {'request':bundle.request_fingerprint, 'frozen':fingerprint(frozen.to_dict()),
                    'candidate_head':bundle.candidate_head, 'candidate_diff':bundle.candidate_diff_sha256}
        artifact_id = bundle.request_id + ':public-witness:' + fingerprint(identity)[:20]
        if self.artifact_store.has_artifact(artifact_id):
            artifact = self.artifact_store.get_artifact(artifact_id)
            raw = decode_strict_json(artifact.content)
            if artifact.source != 'PublicAcceptanceWitness' or raw['identity'] != identity:
                raise JudgeIntegrityError('public witness artifact identity mismatch')
            observations = tuple(PublicWitnessObservationV1(**r) for r in raw['observations'])
        else:
            if self.public_witness_port is None or workspace is None:
                observations = tuple(PublicWitnessObservationV1(o.obligation_id,'UNRESOLVED',
                    detail='Public witness execution unavailable') for o in frozen.obligations if o.kind != 'VERIFICATION')
            else:
                observations = self.public_witness_port.evaluate(frozen=frozen,workspace=workspace,decode=decode_strict_json)
            self.artifact_store.create_artifact(Artifact.from_text(artifact_id=artifact_id,
                kind=ArtifactKind.OTHER, origin=ArtifactOrigin.CONTROL_PLANE,
                content=canonical({'identity':identity,'observations':[o.to_dict() for o in observations]}),
                media_type='application/json',source='PublicAcceptanceWitness',
                created_at=datetime.now(timezone.utc).isoformat(),metadata=identity))
        expected = {o.obligation_id for o in frozen.obligations if o.kind != 'VERIFICATION'}
        if len(observations) != len(expected) or {o.obligation_id for o in observations} != expected:
            raise JudgeIntegrityError('public witness observations do not cover frozen obligations')
        rows = tuple(AcceptanceEvidenceV1(o.obligation_id,
            'UNRESOLVED', (), (),
            ('Public witness '+o.status+': observed='+repr(o.observed_text)+'; '+o.detail).encode('utf-8')[:2048].decode('utf-8',errors='ignore'),
            'NON_INDEPENDENT','candidate_diff') for o in observations if o.status != 'MATCH')
        bound = fingerprint({'request':bundle.request_fingerprint,'public_witness':[o.to_dict() for o in observations]})
        return replace(bundle,semantic_assessment=rows,public_witness_observations=observations,
            request_fingerprint=bound,request_id=f'{bundle.run_id}:judge:{bound[:20]}'), 0

    def review(self, *, run_id: str, contract: Any, lease_id: str) -> JudgeCoordinatorResult:
        bundle = self.resolver.resolve(run_id=run_id, contract=contract, lease_id=lease_id)
        baseline = self.manager.snapshot(lease_id)
        workspace = None
        if self.require_acceptance and self.public_witness_port is not None:
            from ..workspace import Workspace, WorkspaceSessionFactory
            workspace = Workspace(WorkspaceSessionFactory().create(run=self.run_store.get_run(run_id),
                todo=self.run_store.get_todo(contract.todo_id),contract=contract,
                root=self.manager.get_active(lease_id).worktree_root))
        bundle, comparison_calls = self.compare_public_contract(bundle, workspace)
        self._assert_candidate_unchanged(lease_id, baseline)
        existing = self.publisher.find_report(bundle.request_id)
        if existing is not None:
            self.publisher.validate_report(existing, bundle)
            self.publisher.publish_report(existing)
            return JudgeCoordinatorResult(existing, True, 0, 0)
        baseline = self.manager.snapshot(lease_id)
        model_calls = comparison_calls
        reused_turns = 0
        prior_error: str | None = None
        prior_response: str | None = None
        last_error = "judge produced no usable response"
        exhaustion_kind = "UNKNOWN"
        for attempt in range(self.policy.max_model_attempts):
            turn = self.publisher.find_turn(bundle.request_id, attempt)
            if turn is not None:
                reused_turns += 1
            else:
                system_prompt, user_prompt = self.prompts.prompts(bundle, prior_error=prior_error, prior_response=prior_response)
                try:
                    response = self.runtime.complete(system_prompt, user_prompt, "json")
                    model_calls += 1
                    if response.model_id != self.policy.model_id:
                        raise JudgeProtocolError("judge response came from unexpected model")
                    if not isinstance(response.content, str) or len(response.content.encode("utf-8")) > self.policy.max_response_bytes:
                        raise JudgeProtocolError("judge response is missing or exceeds max_response_bytes")
                    turn = JudgeTurnRecord(bundle.request_id, attempt, JudgeTurnStatus.RESPONSE, response.model_id, response.model_tag, response.content, None)
                except LeadModelError as exc:
                    model_calls += 1
                    turn = JudgeTurnRecord(bundle.request_id, attempt, JudgeTurnStatus.EXECUTION_ERROR, self.policy.model_id, None, None, f"{type(exc).__name__}: {exc}")
                self.publisher.publish_turn(turn)
                self._assert_candidate_unchanged(lease_id, baseline)
            if turn.status is JudgeTurnStatus.EXECUTION_ERROR:
                exhaustion_kind = "JUDGE_TRANSPORT_EXHAUSTED"
                last_error = turn.error or "judge execution error"
                prior_error, prior_response = last_error, None
                continue
            assert turn.content is not None
            try:
                analysis = parse_judge_response(turn.content, bundle)
            except JudgeProtocolError as exc:
                exhaustion_kind = "JUDGE_PROTOCOL_EXHAUSTED"
                last_error = str(exc)
                prior_error, prior_response = last_error, turn.content
                continue
            raw_id = self.publisher.turn_artifact_id(bundle.request_id, attempt)
            report = JudgeReport(
                report_id=bundle.request_id,
                request_fingerprint=bundle.request_fingerprint,
                run_id=bundle.run_id,
                todo_id=bundle.todo_id,
                verification_report_id=bundle.verification_report.report_id,
                critic_report_id=bundle.critic_report.report_id,
                candidate_diff_sha256=bundle.candidate_diff_sha256,
                analysis=analysis,
                model_id=turn.model_id or self.policy.model_id,
                model_tag=turn.model_tag or "unknown",
                model_attempts=attempt + 1,
                raw_turn_artifact_id=raw_id,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self.publisher.publish_report(report)
            self._assert_candidate_unchanged(lease_id, baseline)
            return JudgeCoordinatorResult(report, False, model_calls, reused_turns)
        raise JudgeExhaustedError(f"judge exhausted {self.policy.max_model_attempts} attempts: {last_error}", failure_kind=exhaustion_kind, request_id=bundle.request_id, attempts=self.policy.max_model_attempts)

    def _assert_candidate_unchanged(self, lease_id: str, baseline: Any) -> None:
        if not self.policy.enforce_no_workspace_mutation:
            raise JudgeIntegrityError("Phase 9 no-mutation policy must remain enabled")
        current = self.manager.snapshot(lease_id)
        if (current.head_commit != baseline.head_commit or current.diff_sha256 != baseline.diff_sha256
                or current.dirty_paths != baseline.dirty_paths or getattr(current, 'content_sha256', None) != getattr(baseline, 'content_sha256', None)):
            raise JudgeIntegrityError("judge execution changed candidate worktree")
