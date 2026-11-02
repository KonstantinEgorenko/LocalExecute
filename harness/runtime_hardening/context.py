'Deterministic bounded prompt view for frozen external evidence synthesis.'
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .policy import RuntimeHardeningPolicy

_PREFIX = "RESEARCH_SYNTHESIS_REQUEST\n"
_ORIGINAL_MARKER = "ORIGINAL_REQUEST\n" + _PREFIX
_PREVIOUS_BEGIN = "previous_response_begin\n"
_PREVIOUS_END = "\nprevious_response_end"


class SynthesisBudgetError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SynthesisCompaction:
    user_prompt: str
    original_bytes: int
    final_bytes: int
    original_sources: int
    selected_sources: int
    original_evidence: int
    selected_evidence: int
    truncated_excerpts: int

    def to_dict(self) -> dict[str, int]:
        return {
            "original_bytes": self.original_bytes,
            "final_bytes": self.final_bytes,
            "original_sources": self.original_sources,
            "selected_sources": self.selected_sources,
            "original_evidence": self.original_evidence,
            "selected_evidence": self.selected_evidence,
            "truncated_excerpts": self.truncated_excerpts,
        }


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _topic_key(value: object) -> str:
    return _text(value).strip().casefold()


def _primary(source: Mapping[str, Any]) -> bool:
    return _text(source.get("tier")).strip().upper() == "PRIMARY"


def _trim_source(source: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(source)
    if "title" in result:
        result["title"] = _text(result["title"])[:512]
    if "url" in result:
        result["url"] = _text(result["url"])[:2048]
    return result


def _rebuild(
    payload: Mapping[str, Any],
    sources_by_id: Mapping[str, Mapping[str, Any]],
    selected: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    used_ids = []
    seen = set()
    for item in selected:
        source_id = _text(item.get("source_id"))
        if source_id and source_id not in seen:
            seen.add(source_id)
            used_ids.append(source_id)
    sources = [
        _trim_source(sources_by_id[sid])
        for sid in used_ids
        if sid in sources_by_id
    ]
    result = dict(payload)
    result["sources"] = sources
    result["evidence"] = list(selected)
    prompt = _PREFIX + _canonical(result)
    return result, prompt


def _compact_initial_synthesis(
    user_prompt: str,
    policy: RuntimeHardeningPolicy,
    *,
    hard_budget: int,
) -> SynthesisCompaction:
    original_bytes = len(user_prompt.encode("utf-8"))
    if not user_prompt.startswith(_PREFIX):
        raise SynthesisBudgetError(
            "canonical synthesis request must start with synthesis prefix"
        )
    try:
        payload = json.loads(user_prompt[len(_PREFIX):])
    except json.JSONDecodeError as exc:
        raise SynthesisBudgetError(
            f"cannot parse synthesis request: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise SynthesisBudgetError(
            "synthesis request payload must be an object"
        )

    sources_raw = payload.get("sources", [])
    evidence_raw = payload.get("evidence", [])
    required_topics = [
        _topic_key(item)
        for item in payload.get("required_topics", [])
        if _topic_key(item)
    ]
    if not isinstance(sources_raw, list) or not isinstance(evidence_raw, list):
        raise SynthesisBudgetError(
            "synthesis sources/evidence must be lists"
        )

    sources = [
        item for item in sources_raw if isinstance(item, Mapping)
    ]
    sources_by_id = {
        _text(item.get("source_id")): item
        for item in sources
        if _text(item.get("source_id")).strip()
    }

    candidates = []
    truncated_excerpts = 0
    for index, raw in enumerate(evidence_raw):
        if not isinstance(raw, Mapping):
            continue
        source_id = _text(raw.get("source_id"))
        evidence_id = _text(raw.get("evidence_id"))
        if (
            not source_id
            or source_id not in sources_by_id
            or not evidence_id
        ):
            continue
        item = dict(raw)
        excerpt = _text(item.get("excerpt"))
        if len(excerpt) > policy.max_excerpt_chars_per_evidence:
            excerpt = excerpt[: policy.max_excerpt_chars_per_evidence]
            truncated_excerpts += 1
        item["excerpt"] = excerpt
        candidates.append(
            {
                "index": index,
                "item": item,
                "source_id": source_id,
                "evidence_id": evidence_id,
                "topic": _topic_key(item.get("topic")),
                "primary": _primary(sources_by_id[source_id]),
            }
        )

    selected: list[dict[str, Any]] = []
    selected_eids: set[str] = set()
    selected_sids: set[str] = set()

    def add(candidate: Mapping[str, Any]) -> bool:
        eid = _text(candidate["evidence_id"])
        sid = _text(candidate["source_id"])
        if eid in selected_eids:
            return False
        if (
            sid not in selected_sids
            and len(selected_sids) >= policy.max_selected_sources
        ):
            return False
        if len(selected) >= policy.max_selected_evidence:
            return False
        selected.append(dict(candidate["item"]))
        selected_eids.add(eid)
        selected_sids.add(sid)
        return True

    ordered = sorted(
        candidates,
        key=lambda candidate: (
            0
            if candidate["primary"] and policy.preserve_primary_first
            else 1,
            candidate["index"],
            candidate["evidence_id"],
        ),
    )

    if policy.preserve_topic_coverage:
        for topic in required_topics:
            for candidate in ordered:
                if (
                    candidate["topic"] == topic
                    and candidate["source_id"] not in selected_sids
                    and add(candidate)
                ):
                    break

    if policy.preserve_source_diversity:
        for candidate in ordered:
            if len(selected) >= policy.max_selected_evidence:
                break
            if candidate["source_id"] in selected_sids:
                continue
            add(candidate)

    for candidate in ordered:
        if len(selected) >= policy.max_selected_evidence:
            break
        add(candidate)

    if evidence_raw and not selected:
        raise SynthesisBudgetError(
            "evidence exists but no bounded evidence can be selected"
        )

    _, prompt = _rebuild(payload, sources_by_id, selected)

    while len(prompt.encode("utf-8")) > hard_budget and selected:
        longest_index = -1
        longest_length = -1
        for index, item in enumerate(selected):
            length = len(_text(item.get("excerpt")))
            if (
                length > policy.min_excerpt_chars_per_evidence
                and length > longest_length
            ):
                longest_index = index
                longest_length = length

        if longest_index >= 0:
            current = _text(selected[longest_index].get("excerpt"))
            target = max(
                policy.min_excerpt_chars_per_evidence,
                int(len(current) * 0.75),
            )
            if target >= len(current):
                target = len(current) - 1
            selected[longest_index]["excerpt"] = current[:target]
            truncated_excerpts += 1
        else:
            selected.pop()

        _, prompt = _rebuild(payload, sources_by_id, selected)

    final_bytes = len(prompt.encode("utf-8"))
    if final_bytes > hard_budget:
        raise SynthesisBudgetError(
            "non-evidence synthesis payload exceeds hard budget: "
            f"{final_bytes}>{hard_budget}"
        )
    if evidence_raw and not selected:
        raise SynthesisBudgetError(
            "hard budget would require dropping all evidence"
        )

    selected_source_ids = {
        _text(item.get("source_id"))
        for item in selected
        if _text(item.get("source_id"))
    }
    return SynthesisCompaction(
        user_prompt=prompt,
        original_bytes=original_bytes,
        final_bytes=final_bytes,
        original_sources=len(sources_raw),
        selected_sources=len(selected_source_ids),
        original_evidence=len(evidence_raw),
        selected_evidence=len(selected),
        truncated_excerpts=truncated_excerpts,
    )


def _bound_previous_response(
    envelope: str,
    policy: RuntimeHardeningPolicy,
) -> str:
    start = envelope.find(_PREVIOUS_BEGIN)
    if start < 0:
        return envelope
    content_start = start + len(_PREVIOUS_BEGIN)
    end = envelope.find(_PREVIOUS_END, content_start)
    if end < 0:
        raise SynthesisBudgetError(
            "retry envelope has unterminated previous_response"
        )

    previous = envelope[content_start:end]
    bounded = previous[: policy.max_retry_previous_response_chars]
    return (
        envelope[:content_start]
        + bounded
        + envelope[end:]
    )


def _compact_retry_synthesis(
    user_prompt: str,
    policy: RuntimeHardeningPolicy,
) -> SynthesisCompaction:
    marker_index = user_prompt.rfind(_ORIGINAL_MARKER)
    if marker_index < 0:
        raise SynthesisBudgetError(
            "synthesis retry envelope lacks canonical ORIGINAL_REQUEST marker"
        )

    nested_start = marker_index + len("ORIGINAL_REQUEST\n")
    envelope = _bound_previous_response(
        user_prompt[:nested_start],
        policy,
    )
    envelope_bytes = len(envelope.encode("utf-8"))
    available = policy.max_synthesis_user_utf8_bytes - envelope_bytes
    if available < 4096:
        raise SynthesisBudgetError(
            "synthesis retry envelope leaves insufficient "
            f"original-request budget: {available} bytes"
        )

    nested = user_prompt[nested_start:]
    compacted = _compact_initial_synthesis(
        nested,
        policy,
        hard_budget=available,
    )
    rebuilt = envelope + compacted.user_prompt
    final_bytes = len(rebuilt.encode("utf-8"))
    if final_bytes > policy.max_synthesis_user_utf8_bytes:
        raise SynthesisBudgetError(
            "synthesis retry exceeds hard budget after compaction: "
            f"{final_bytes}>{policy.max_synthesis_user_utf8_bytes}"
        )

    return SynthesisCompaction(
        user_prompt=rebuilt,
        original_bytes=len(user_prompt.encode("utf-8")),
        final_bytes=final_bytes,
        original_sources=compacted.original_sources,
        selected_sources=compacted.selected_sources,
        original_evidence=compacted.original_evidence,
        selected_evidence=compacted.selected_evidence,
        truncated_excerpts=compacted.truncated_excerpts,
    )


def compact_synthesis_user_prompt(
    user_prompt: str,
    policy: RuntimeHardeningPolicy,
) -> SynthesisCompaction | None:
    # Both research reasoning protocol repair and research contracts semantic repair preserve
    # ORIGINAL_REQUEST. Retry envelopes must therefore be budgeted even when
    # they do not themselves start with RESEARCH_SYNTHESIS_REQUEST.
    if _ORIGINAL_MARKER in user_prompt:
        return _compact_retry_synthesis(user_prompt, policy)

    if not user_prompt.startswith(_PREFIX):
        return None

    body = user_prompt[len(_PREFIX):]
    if body.lstrip().startswith("{"):
        return _compact_initial_synthesis(
            user_prompt,
            policy,
            hard_budget=policy.max_synthesis_user_utf8_bytes,
        )

    raise SynthesisBudgetError(
        "non-canonical synthesis prompt lacks ORIGINAL_REQUEST marker"
    )
