"""Research-quality ablations over evidence multiplicity and source independence."""
from __future__ import annotations

from typing import Mapping

from .identity import SourceIdentityResolver


def independence_ablation(report: Mapping) -> dict:
    sources = {
        str(item.get("source_id")): SourceIdentityResolver().resolve(dict(item))
        for item in report.get("sources", [])
        if isinstance(item, Mapping) and item.get("source_id")
    }
    evidence = {
        str(item.get("evidence_id")): item
        for item in report.get("evidence", [])
        if isinstance(item, Mapping) and item.get("evidence_id")
    }

    rows = []
    for claim in report.get("claims", []):
        if not isinstance(claim, Mapping):
            continue
        refs = [item for item in claim.get("evidence", []) if isinstance(item, Mapping)]
        source_ids = set()
        docs = set()
        families = set()
        for ref in refs:
            ev = evidence.get(str(ref.get("evidence_id")))
            if not isinstance(ev, Mapping):
                continue
            sid = str(ev.get("source_id") or ev.get("source") or ev.get("source_ref") or "")
            if not sid:
                continue
            source_ids.add(sid)
            ident = sources.get(sid)
            if ident:
                docs.add(ident.document_key)
                families.add(ident.source_family)
        rows.append({
            "claim_id": str(claim.get("claim_id", "")),
            "importance": str(claim.get("importance", "")),
            "raw_evidence_refs": len(refs),
            "unique_source_ids": len(source_ids),
            "unique_documents": len(docs),
            "unique_source_families": len(families),
        })
    return {
        "schema_version": 1,
        "claims": rows,
        "important_fragment_false_confidence_cases": sum(
            row["importance"] == "IMPORTANT"
            and row["raw_evidence_refs"] >= 2
            and row["unique_documents"] < 2
            for row in rows
        ),
    }
