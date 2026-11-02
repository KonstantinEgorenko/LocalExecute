'execution profiles fixed-profile coding attempt loop with FINAL-only grace.'
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from harness.agent_loop.errors import AgentProtocolError
from harness.executor.models import ExecutionMessage
from harness.workspace.models import ToolStatus, WorkspaceSession
from harness.executor.profiles import ExecutionProfile, ExecutionProfilePolicy
from harness.executor.finalization import evaluate_finalization_eligibility, final_only_json_schema_v2, finalization_message_v2
from harness.executor.profile_client import ProfileGeneration
from harness.executor.profile_prompt import direct_task_messages_v2, protocol_error_message_v2, tool_feedback_message_v2
from harness.executor.profile_protocol import parse_agent_action_v2
from harness.executor.profile_tool_adapter import ToolProtocolV2Adapter
from harness.executor.profile_tool_capabilities import V2ToolCapabilities, capabilities_for_session
from harness.executor.profile_tool_models import V2ActionKind, V2ToolFeedback
from harness.executor.profile_tool_schema import agent_action_json_schema_v2


class ProfileAttemptOutcome(StrEnum):
    FINALIZED = "FINALIZED"
    PRODUCTIVE_QUOTA_EXHAUSTED = "PRODUCTIVE_QUOTA_EXHAUSTED"
    FINALIZATION_FAILED = "FINALIZATION_FAILED"
    PROTOCOL_FAILURE_LIMIT = "PROTOCOL_FAILURE_LIMIT"
    TOOL_FAILURE_LIMIT = "TOOL_FAILURE_LIMIT"
    TOOL_CALL_LIMIT = "TOOL_CALL_LIMIT"
    COMPLETION_GUARD_FAILED = "COMPLETION_GUARD_FAILED"


class ProfileChatClient(Protocol):
    def chat_profile(
        self,
        *,
        base_url: str,
        profile: ExecutionProfile,
        messages: tuple[ExecutionMessage, ...],
        response_schema: dict,
    ) -> ProfileGeneration: ...


@dataclass(frozen=True, slots=True)
class ProfileAttemptResult:
    outcome: ProfileAttemptOutcome
    profile_id: str
    model_id: str
    model_tag: str
    final_summary: str | None
    model_calls: int
    productive_calls: int
    finalization_calls: int
    tool_calls: int
    protocol_failures: int
    tool_failures: int
    changed_paths: tuple[str, ...]
    prompt_eval_count: int
    eval_count: int
    finalization_reason: str | None
    reason: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ProfileAttemptOutcome):
            raise ValueError("outcome must be ProfileAttemptOutcome")
        for name in ("profile_id", "model_id", "model_tag"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty string")
        for name in (
            "model_calls",
            "productive_calls",
            "finalization_calls",
            "tool_calls",
            "protocol_failures",
            "tool_failures",
            "prompt_eval_count",
            "eval_count",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be integer >= 0")
        if self.model_calls != self.productive_calls + self.finalization_calls:
            raise ValueError("model_calls must equal productive + finalization calls")
        if self.finalization_calls > 1:
            raise ValueError("finalization_calls must remain <= 1")
        if not isinstance(self.changed_paths, tuple) or any(
            not isinstance(path, str) or not path for path in self.changed_paths
        ):
            raise ValueError("changed_paths must be tuple[str, ...]")
        if len(self.changed_paths) != len(set(self.changed_paths)):
            raise ValueError("changed_paths must not contain duplicates")
        if self.final_summary is not None and (
            not isinstance(self.final_summary, str) or not self.final_summary.strip()
        ):
            raise ValueError("final_summary must be non-empty str|None")
        for name in ("finalization_reason", "reason"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be non-empty str|None")

    @property
    def finalized(self) -> bool:
        return self.outcome is ProfileAttemptOutcome.FINALIZED

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "profile_id": self.profile_id,
            "model_id": self.model_id,
            "model_tag": self.model_tag,
            "final_summary": self.final_summary,
            "model_calls": self.model_calls,
            "productive_calls": self.productive_calls,
            "finalization_calls": self.finalization_calls,
            "tool_calls": self.tool_calls,
            "protocol_failures": self.protocol_failures,
            "tool_failures": self.tool_failures,
            "changed_paths": list(self.changed_paths),
            "prompt_eval_count": self.prompt_eval_count,
            "eval_count": self.eval_count,
            "finalization_reason": self.finalization_reason,
            "reason": self.reason,
            "finalized": self.finalized,
        }


class ExecutionProfileAttemptRunner:
    """Execute one model/profile for one bounded attempt; never route or switch."""

    def __init__(
        self,
        *,
        client: ProfileChatClient,
        base_url: str,
        policy: ExecutionProfilePolicy,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.startswith("http"):
            raise ValueError("base_url must be HTTP URL")
        if not isinstance(policy, ExecutionProfilePolicy):
            raise ValueError("policy must be Phase13ExecutionPolicy")
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.policy = policy

    @staticmethod
    def _effective_capabilities(
        profile: ExecutionProfile, session: WorkspaceSession
    ) -> V2ToolCapabilities:
        workspace = capabilities_for_session(session)
        allowed = tuple(tool for tool in workspace.tools if tool in profile.allowed_tools)
        if not allowed:
            raise ValueError("profile/workspace capability intersection is empty")
        return V2ToolCapabilities(
            allowed,
            can_mutate=workspace.can_mutate and any(
                tool.value in {"CREATE_TEXT", "WRITE_TEXT", "REPLACE_TEXT", "DELETE_FILE"}
                for tool in allowed
            ),
            can_run_commands=False,
        )

    def _result(
        self,
        *,
        outcome: ProfileAttemptOutcome,
        profile: ExecutionProfile,
        final_summary: str | None,
        productive_calls: int,
        finalization_calls: int,
        tool_calls: int,
        protocol_failures: int,
        tool_failures: int,
        changed_paths: list[str],
        prompt_eval_count: int,
        eval_count: int,
        finalization_reason: str | None = None,
        reason: str | None = None,
    ) -> ProfileAttemptResult:
        return ProfileAttemptResult(
            outcome=outcome,
            profile_id=profile.profile_id,
            model_id=profile.model_id,
            model_tag=profile.model_tag,
            final_summary=final_summary,
            model_calls=productive_calls + finalization_calls,
            productive_calls=productive_calls,
            finalization_calls=finalization_calls,
            tool_calls=tool_calls,
            protocol_failures=protocol_failures,
            tool_failures=tool_failures,
            changed_paths=tuple(changed_paths),
            prompt_eval_count=prompt_eval_count,
            eval_count=eval_count,
            finalization_reason=finalization_reason,
            reason=reason,
        )

    def run(
        self,
        *,
        profile: ExecutionProfile,
        workspace_session: WorkspaceSession,
        objective: str,
        contract_max_worker_calls: int,
        requires_workspace_mutation: bool = True,
    ) -> ProfileAttemptResult:
        if not isinstance(profile, ExecutionProfile):
            raise ValueError("profile must be ExecutionProfile")
        if not isinstance(workspace_session, WorkspaceSession):
            raise ValueError("workspace_session must be WorkspaceSession")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("objective must be non-empty string")
        if not isinstance(contract_max_worker_calls, int) or isinstance(
            contract_max_worker_calls, bool
        ) or contract_max_worker_calls <= 0:
            raise ValueError("contract_max_worker_calls must be integer > 0")
        if profile.budget.max_total_model_calls > contract_max_worker_calls:
            raise ValueError("profile total call budget exceeds attempt contract")
        if contract_max_worker_calls > self.policy.max_worker_calls_per_attempt:
            raise ValueError("attempt contract exceeds Phase 13C policy")
        if not isinstance(requires_workspace_mutation, bool):
            raise ValueError("requires_workspace_mutation must be bool")

        capabilities = self._effective_capabilities(profile, workspace_session)
        adapter = ToolProtocolV2Adapter(workspace_session)
        messages = direct_task_messages_v2(
            objective,
            workspace_session,
            requires_workspace_mutation=requires_workspace_mutation,
        )
        # Replace the generic profile tools tool listing with a profile-specific
        # schema authority. The prompt may mention a workspace-superset tool,
        # but strict structured decoding/parser below permits only intersection.
        schema = agent_action_json_schema_v2(capabilities)

        productive_calls = 0
        finalization_calls = 0
        tool_calls = 0
        protocol_failures = 0
        tool_failures = 0
        changed_paths: list[str] = []
        last_feedback: V2ToolFeedback | None = None
        prompt_eval_count = 0
        eval_count = 0

        for _ in range(profile.budget.max_productive_calls):
            generation = self.client.chat_profile(
                base_url=self.base_url,
                profile=profile,
                messages=messages,
                response_schema=schema,
            )
            productive_calls += 1
            prompt_eval_count += generation.prompt_eval_count
            eval_count += generation.eval_count
            if generation.model_tag != profile.model_tag:
                raise RuntimeError("profile client returned a different model tag")
            messages = (*messages, ExecutionMessage("assistant", generation.content))

            try:
                action = parse_agent_action_v2(generation.content, capabilities)
            except AgentProtocolError as exc:
                protocol_failures += 1
                if protocol_failures > profile.budget.max_protocol_failures:
                    return self._result(
                        outcome=ProfileAttemptOutcome.PROTOCOL_FAILURE_LIMIT,
                        profile=profile,
                        final_summary=None,
                        productive_calls=productive_calls,
                        finalization_calls=finalization_calls,
                        tool_calls=tool_calls,
                        protocol_failures=protocol_failures,
                        tool_failures=tool_failures,
                        changed_paths=changed_paths,
                        prompt_eval_count=prompt_eval_count,
                        eval_count=eval_count,
                        reason="profile protocol failure limit reached",
                    )
                messages = (*messages, protocol_error_message_v2(str(exc)))
                continue

            if action.action is V2ActionKind.FINAL:
                if requires_workspace_mutation and not changed_paths:
                    return self._result(
                        outcome=ProfileAttemptOutcome.COMPLETION_GUARD_FAILED,
                        profile=profile,
                        final_summary=None,
                        productive_calls=productive_calls,
                        finalization_calls=finalization_calls,
                        tool_calls=tool_calls,
                        protocol_failures=protocol_failures,
                        tool_failures=tool_failures,
                        changed_paths=changed_paths,
                        prompt_eval_count=prompt_eval_count,
                        eval_count=eval_count,
                        reason="FINAL before required workspace mutation",
                    )
                return self._result(
                    outcome=ProfileAttemptOutcome.FINALIZED,
                    profile=profile,
                    final_summary=action.final.summary,
                    productive_calls=productive_calls,
                    finalization_calls=finalization_calls,
                    tool_calls=tool_calls,
                    protocol_failures=protocol_failures,
                    tool_failures=tool_failures,
                    changed_paths=changed_paths,
                    prompt_eval_count=prompt_eval_count,
                    eval_count=eval_count,
                    reason=None,
                )

            if tool_calls >= profile.budget.max_tool_calls:
                return self._result(
                    outcome=ProfileAttemptOutcome.TOOL_CALL_LIMIT,
                    profile=profile,
                    final_summary=None,
                    productive_calls=productive_calls,
                    finalization_calls=finalization_calls,
                    tool_calls=tool_calls,
                    protocol_failures=protocol_failures,
                    tool_failures=tool_failures,
                    changed_paths=changed_paths,
                    prompt_eval_count=prompt_eval_count,
                    eval_count=eval_count,
                    reason="profile tool call limit reached",
                )

            tool_calls += 1
            last_feedback = adapter.execute(
                f"{workspace_session.run_id}:{profile.profile_id}:tool:{tool_calls}",
                action.tool_call,
            )
            for path in last_feedback.changed_paths:
                if path not in changed_paths:
                    changed_paths.append(path)
            if last_feedback.status is not ToolStatus.SUCCESS:
                tool_failures += 1
                if tool_failures > profile.budget.max_tool_failures:
                    return self._result(
                        outcome=ProfileAttemptOutcome.TOOL_FAILURE_LIMIT,
                        profile=profile,
                        final_summary=None,
                        productive_calls=productive_calls,
                        finalization_calls=finalization_calls,
                        tool_calls=tool_calls,
                        protocol_failures=protocol_failures,
                        tool_failures=tool_failures,
                        changed_paths=changed_paths,
                        prompt_eval_count=prompt_eval_count,
                        eval_count=eval_count,
                        reason="profile tool failure limit reached",
                    )
            messages = (*messages, tool_feedback_message_v2(last_feedback))

        eligibility = evaluate_finalization_eligibility(
            profile=profile,
            policy=self.policy,
            productive_calls_used=productive_calls,
            total_model_calls_used=productive_calls,
            contract_max_worker_calls=contract_max_worker_calls,
            changed_paths=tuple(changed_paths),
            last_feedback=last_feedback,
        )
        if not eligibility.eligible:
            return self._result(
                outcome=ProfileAttemptOutcome.PRODUCTIVE_QUOTA_EXHAUSTED,
                profile=profile,
                final_summary=None,
                productive_calls=productive_calls,
                finalization_calls=0,
                tool_calls=tool_calls,
                protocol_failures=protocol_failures,
                tool_failures=tool_failures,
                changed_paths=changed_paths,
                prompt_eval_count=prompt_eval_count,
                eval_count=eval_count,
                finalization_reason=eligibility.reason,
                reason="productive model-call quota exhausted",
            )

        messages = (*messages, finalization_message_v2())
        generation = self.client.chat_profile(
            base_url=self.base_url,
            profile=profile,
            messages=messages,
            response_schema=final_only_json_schema_v2(),
        )
        finalization_calls = 1
        prompt_eval_count += generation.prompt_eval_count
        eval_count += generation.eval_count
        if generation.model_tag != profile.model_tag:
            raise RuntimeError("finalization switched model tag")
        try:
            action = parse_agent_action_v2(generation.content, capabilities)
        except AgentProtocolError as exc:
            return self._result(
                outcome=ProfileAttemptOutcome.FINALIZATION_FAILED,
                profile=profile,
                final_summary=None,
                productive_calls=productive_calls,
                finalization_calls=finalization_calls,
                tool_calls=tool_calls,
                protocol_failures=protocol_failures + 1,
                tool_failures=tool_failures,
                changed_paths=changed_paths,
                prompt_eval_count=prompt_eval_count,
                eval_count=eval_count,
                finalization_reason=eligibility.reason,
                reason=f"FINAL-only grace protocol failure: {exc}",
            )
        if action.action is not V2ActionKind.FINAL:
            return self._result(
                outcome=ProfileAttemptOutcome.FINALIZATION_FAILED,
                profile=profile,
                final_summary=None,
                productive_calls=productive_calls,
                finalization_calls=finalization_calls,
                tool_calls=tool_calls,
                protocol_failures=protocol_failures,
                tool_failures=tool_failures,
                changed_paths=changed_paths,
                prompt_eval_count=prompt_eval_count,
                eval_count=eval_count,
                finalization_reason=eligibility.reason,
                reason="FINAL-only grace returned TOOL action",
            )
        return self._result(
            outcome=ProfileAttemptOutcome.FINALIZED,
            profile=profile,
            final_summary=action.final.summary,
            productive_calls=productive_calls,
            finalization_calls=finalization_calls,
            tool_calls=tool_calls,
            protocol_failures=protocol_failures,
            tool_failures=tool_failures,
            changed_paths=changed_paths,
            prompt_eval_count=prompt_eval_count,
            eval_count=eval_count,
            finalization_reason=eligibility.reason,
            reason=None,
        )
