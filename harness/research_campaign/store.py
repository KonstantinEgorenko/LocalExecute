'SQLite durable experiment ledger for research campaign research campaigns.\n\nSQLite owns structured research metadata. Large logs, models and submissions remain\nin external artifact storage and are referenced by immutable strings only.\n'
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .campaign import ResearchCampaignStateV1, ResearchCampaignStatusV1
from .candidate_evaluation import (
    CandidateEvaluationRegistrationV1,
    CandidateEvaluationResultV1,
    CandidateEvaluationSpecV1,
    CandidateEvaluationStatusV1,
    CandidateEvaluationSurfaceV1,
    StoredCandidateEvaluationV1,
)
from .contracts import EvaluationContractV1, MetricDirectionV1, ResearchContractV1
from .decisions import SearchDecisionActionV1, SearchDecisionV1
from .errors import (
    ResearchStoreConflictError,
    ResearchStoreError,
    ResearchStoreIntegrityError,
    ResearchStoreNotFoundError,
    ResearchStoreSchemaError,
)
from .insights import (
    ResearchInsightDispositionV1,
    ResearchInsightEvidenceKindV1,
    ResearchInsightRecordV1,
    ResearchInsightRegistrationV1,
    ResearchInsightSurfaceV1,
    experiment_result_evidence_fingerprint,
    gate_evaluation_evidence_fingerprint,
    repair_outcome_evidence_fingerprint,
)
from .models import (
    ExperimentResultV1,
    ExperimentSpecV1,
    ExperimentStatusV1,
    HypothesisStatusV1,
    HypothesisV1,
    ResearchInsightV1,
)
from .repair import GateFailureEvidenceV1, RepairProposalV1, RepairRegistrationV1
from .evidence import ResearchEvidencePacketV1
from .stop_policy import ResearchStopDecisionV1

from .serialization import canonical_json, safe_id, sha256_hex, stable_sha256, timestamp

_SCHEMA_VERSION = 7
_TERMINAL_STATUSES = {
    ExperimentStatusV1.SUCCEEDED,
    ExperimentStatusV1.FAILED,
    ExperimentStatusV1.CRASHED,
    ExperimentStatusV1.PRUNED,
    ExperimentStatusV1.INVALID,
    ExperimentStatusV1.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class ExperimentRegistrationV1:
    requested_experiment_id: str
    canonical_experiment_id: str
    fingerprint: str
    created: bool
    reused_existing: bool


@dataclass(frozen=True, slots=True)
class StoredExperimentV1:
    spec: ExperimentSpecV1
    result: ExperimentResultV1 | None


@dataclass(frozen=True, slots=True)
class ResearchCampaignSummaryV1:
    campaign_id: str
    contract_hash: str
    experiment_count: int
    terminal_count: int
    succeeded_count: int
    failed_count: int
    running_count: int
    planned_count: int
    champion_experiment_id: str | None
    best_experiment_id: str | None
    best_primary_metric: float | None
    comparable_evaluation_contract_hash: str | None


class ResearchExperimentStore:
    """Single-user/local SQLite ledger with transactional writes and indexed queries."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(str(self.path), timeout=30.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as exc:
            raise ResearchStoreError(f"cannot open research SQLite store {self.path}: {exc}") from exc

    def _initialize(self) -> None:
        with self._connect() as connection:
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in (0, 1, 2, 3, 4, 5, 6, _SCHEMA_VERSION):
                    raise ResearchStoreSchemaError(
                        f"unsupported research store schema version {version}; expected {_SCHEMA_VERSION}"
                    )
                if version == 0:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.executescript(
                        """
                        BEGIN IMMEDIATE;
                        CREATE TABLE research_campaigns (
                            campaign_id TEXT PRIMARY KEY,
                            current_contract_hash TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            payload_hash TEXT NOT NULL
                        );
                        CREATE TABLE research_campaign_state (
                            campaign_id TEXT PRIMARY KEY REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                            generation INTEGER NOT NULL,
                            status TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            payload_hash TEXT NOT NULL
                        );
                        CREATE TABLE research_contracts (
                            contract_hash TEXT PRIMARY KEY,
                            campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                            contract_version INTEGER NOT NULL,
                            amends_contract_hash TEXT,
                            created_at TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            payload_hash TEXT NOT NULL,
                            UNIQUE(campaign_id, contract_version)
                        );
                        CREATE TABLE evaluation_contracts (
                            contract_hash TEXT PRIMARY KEY,
                            metric_name TEXT NOT NULL,
                            direction TEXT NOT NULL,
                            dataset_fingerprint TEXT NOT NULL,
                            split_fingerprint TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            payload_hash TEXT NOT NULL
                        );
                        CREATE TABLE hypotheses (
                            hypothesis_id TEXT PRIMARY KEY,
                            campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                            status TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            payload_hash TEXT NOT NULL
                        );
                        CREATE INDEX idx_hypotheses_campaign_status ON hypotheses(campaign_id, status);
                        CREATE TABLE experiments (
                            experiment_id TEXT PRIMARY KEY,
                            campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                            hypothesis_id TEXT NOT NULL REFERENCES hypotheses(hypothesis_id),
                            fingerprint TEXT NOT NULL,
                            strategy_id TEXT NOT NULL,
                            model_id TEXT,
                            provider TEXT,
                            model_version TEXT,
                            evaluation_contract_hash TEXT NOT NULL REFERENCES evaluation_contracts(contract_hash),
                            dataset_fingerprint TEXT NOT NULL,
                            seed INTEGER NOT NULL,
                            created_at TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            payload_hash TEXT NOT NULL,
                            UNIQUE(campaign_id, fingerprint)
                        );
                        CREATE INDEX idx_experiments_campaign ON experiments(campaign_id, created_at);
                        CREATE INDEX idx_experiments_hypothesis ON experiments(hypothesis_id);
                        CREATE INDEX idx_experiments_strategy ON experiments(campaign_id, strategy_id);
                        CREATE INDEX idx_experiments_model ON experiments(campaign_id, model_id);
                        CREATE INDEX idx_experiments_eval ON experiments(campaign_id, evaluation_contract_hash);
                        CREATE TABLE experiment_relations (
                            experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
                            parent_experiment_id TEXT NOT NULL,
                            ordinal INTEGER NOT NULL,
                            PRIMARY KEY(experiment_id, parent_experiment_id)
                        );
                        CREATE TABLE experiment_results (
                            experiment_id TEXT PRIMARY KEY REFERENCES experiments(experiment_id) ON DELETE CASCADE,
                            campaign_id TEXT NOT NULL,
                            status TEXT NOT NULL,
                            primary_metric REAL,
                            started_at TEXT,
                            ended_at TEXT,
                            decision TEXT,
                            failure_kind TEXT,
                            payload_json TEXT NOT NULL,
                            payload_hash TEXT NOT NULL
                        );
                        CREATE INDEX idx_results_campaign_status ON experiment_results(campaign_id, status);
                        CREATE INDEX idx_results_metric ON experiment_results(campaign_id, primary_metric);
                        CREATE TABLE experiment_metrics (
                            experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
                            metric_name TEXT NOT NULL,
                            metric_value REAL NOT NULL,
                            is_primary INTEGER NOT NULL CHECK(is_primary IN (0,1)),
                            PRIMARY KEY(experiment_id, metric_name)
                        );
                        CREATE INDEX idx_metrics_name_value ON experiment_metrics(metric_name, metric_value);
                        CREATE TABLE experiment_artifacts (
                            experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id) ON DELETE CASCADE,
                            artifact_ref TEXT NOT NULL,
                            artifact_kind TEXT NOT NULL,
                            ordinal INTEGER NOT NULL,
                            PRIMARY KEY(experiment_id, artifact_kind, artifact_ref)
                        );
                        CREATE TABLE search_decisions (
                            decision_id TEXT PRIMARY KEY,
                            campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                            experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                            action TEXT NOT NULL,
                            incumbent_experiment_id TEXT,
                            created_at TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            payload_hash TEXT NOT NULL
                        );
                        CREATE INDEX idx_decisions_campaign_time ON search_decisions(campaign_id, created_at, decision_id);
                        CREATE INDEX idx_decisions_campaign_action ON search_decisions(campaign_id, action);
                        CREATE UNIQUE INDEX idx_decisions_campaign_experiment ON search_decisions(campaign_id, experiment_id);
                        CREATE TABLE research_insights (
                            insight_id TEXT PRIMARY KEY,
                            campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                            created_at TEXT NOT NULL,
                            confidence REAL NOT NULL,
                            scope TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            payload_hash TEXT NOT NULL
                        );
                        CREATE INDEX idx_insights_campaign ON research_insights(campaign_id, created_at);
                        CREATE TABLE human_requests_link (
                            campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                            request_id TEXT NOT NULL,
                            experiment_id TEXT,
                            created_at TEXT NOT NULL,
                            PRIMARY KEY(campaign_id, request_id)
                        );
                        PRAGMA user_version = 2;
                        COMMIT;
                        """
                    )
                    connection.execute("BEGIN IMMEDIATE")
                    self._create_candidate_evaluation_schema(connection)
                    self._create_repair_schema(connection)
                    self._create_typed_research_insight_schema(connection)
                    self._create_strategy_checkpoint_schema(connection)
                    connection.execute("PRAGMA user_version = 6")
                    connection.commit()
                elif version == 1:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """CREATE TABLE research_campaign_state (
                            campaign_id TEXT PRIMARY KEY REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                            generation INTEGER NOT NULL,
                            status TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            payload_hash TEXT NOT NULL
                        )"""
                    )
                    connection.execute(
                        "CREATE UNIQUE INDEX idx_decisions_campaign_experiment ON search_decisions(campaign_id, experiment_id)"
                    )
                    rows = connection.execute(
                        "SELECT campaign_id,created_at FROM research_campaigns ORDER BY campaign_id"
                    ).fetchall()
                    for row in rows:
                        state = ResearchCampaignStateV1(
                            campaign_id=str(row["campaign_id"]),
                            status=ResearchCampaignStatusV1.CREATED,
                            generation=0,
                            updated_at=str(row["created_at"]),
                        )
                        payload_json, payload_hash = self._encode(state.to_dict())
                        connection.execute(
                            """INSERT INTO research_campaign_state(
                                campaign_id,generation,status,updated_at,payload_json,payload_hash
                            ) VALUES(?,?,?,?,?,?)""",
                            (
                                state.campaign_id,
                                state.generation,
                                state.status.value,
                                state.updated_at,
                                payload_json,
                                payload_hash,
                            ),
                        )
                    self._create_candidate_evaluation_schema(connection)
                    self._create_repair_schema(connection)
                    self._create_typed_research_insight_schema(connection)
                    self._create_strategy_checkpoint_schema(connection)
                    connection.execute("PRAGMA user_version = 6")
                    connection.commit()
                elif version == 2:
                    connection.execute("BEGIN IMMEDIATE")
                    self._create_candidate_evaluation_schema(connection)
                    self._create_repair_schema(connection)
                    self._create_typed_research_insight_schema(connection)
                    self._create_strategy_checkpoint_schema(connection)
                    connection.execute("PRAGMA user_version = 6")
                    connection.commit()
                elif version == 3:
                    connection.execute("BEGIN IMMEDIATE")
                    self._create_repair_schema(connection)
                    self._create_typed_research_insight_schema(connection)
                    self._create_strategy_checkpoint_schema(connection)
                    connection.execute("PRAGMA user_version = 6")
                    connection.commit()
                elif version == 4:
                    connection.execute("BEGIN IMMEDIATE")
                    self._create_typed_research_insight_schema(connection)
                    self._create_strategy_checkpoint_schema(connection)
                    connection.execute("PRAGMA user_version = 6")
                    connection.commit()
                elif version == 5:
                    connection.execute("BEGIN IMMEDIATE")
                    self._create_strategy_checkpoint_schema(connection)
                    connection.execute("PRAGMA user_version = 6")
                    connection.commit()
                if version < _SCHEMA_VERSION:
                    connection.execute("BEGIN IMMEDIATE")
                    self._create_campaign_cycle_schema(connection)
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                    connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreSchemaError(f"cannot initialize research store schema: {exc}") from exc

    @staticmethod
    def _create_candidate_evaluation_schema(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS candidate_evaluations (
                evaluation_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                source_experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                source_experiment_fingerprint TEXT NOT NULL,
                candidate_commit TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                proposal_kind TEXT NOT NULL,
                surface TEXT NOT NULL,
                evaluation_contract_hash TEXT NOT NULL REFERENCES evaluation_contracts(contract_hash),
                seed INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                UNIQUE(campaign_id, fingerprint)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_candidate_evaluations_campaign ON candidate_evaluations(campaign_id, created_at, evaluation_id)",
            "CREATE INDEX IF NOT EXISTS idx_candidate_evaluations_source ON candidate_evaluations(source_experiment_id, surface)",
            "CREATE INDEX IF NOT EXISTS idx_candidate_evaluations_surface ON candidate_evaluations(campaign_id, surface, created_at)",
            """CREATE TABLE IF NOT EXISTS candidate_evaluation_results (
                evaluation_id TEXT PRIMARY KEY REFERENCES candidate_evaluations(evaluation_id) ON DELETE CASCADE,
                campaign_id TEXT NOT NULL,
                status TEXT NOT NULL,
                primary_metric REAL,
                started_at TEXT,
                ended_at TEXT,
                failure_kind TEXT,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_candidate_evaluation_results_status ON candidate_evaluation_results(campaign_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_candidate_evaluation_results_metric ON candidate_evaluation_results(campaign_id, primary_metric)",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _create_repair_schema(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS repair_proposals (
                repair_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                source_evaluation_id TEXT NOT NULL REFERENCES candidate_evaluations(evaluation_id),
                source_experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                source_candidate_commit TEXT NOT NULL,
                evidence_fingerprint TEXT NOT NULL,
                repair_hypothesis_id TEXT NOT NULL REFERENCES hypotheses(hypothesis_id),
                repair_experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                repair_experiment_fingerprint TEXT NOT NULL,
                repair_depth INTEGER NOT NULL CHECK(repair_depth > 0),
                fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                UNIQUE(campaign_id, source_evaluation_id),
                UNIQUE(campaign_id, fingerprint),
                UNIQUE(campaign_id, repair_experiment_id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_repair_proposals_campaign ON repair_proposals(campaign_id, created_at, repair_id)",
            "CREATE INDEX IF NOT EXISTS idx_repair_proposals_source ON repair_proposals(campaign_id, source_experiment_id)",
            "CREATE INDEX IF NOT EXISTS idx_repair_proposals_experiment ON repair_proposals(repair_experiment_id)",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _create_typed_research_insight_schema(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS typed_research_insights (
                insight_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL,
                polarity TEXT NOT NULL,
                disposition TEXT NOT NULL,
                surface TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                evaluation_contract_hash TEXT NOT NULL REFERENCES evaluation_contracts(contract_hash),
                hypothesis_id TEXT,
                strategy_id TEXT,
                confidence REAL NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                UNIQUE(campaign_id, fingerprint)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_typed_insights_campaign ON typed_research_insights(campaign_id, created_at, insight_id)",
            "CREATE INDEX IF NOT EXISTS idx_typed_insights_kind ON typed_research_insights(campaign_id, kind, disposition)",
            "CREATE INDEX IF NOT EXISTS idx_typed_insights_surface ON typed_research_insights(campaign_id, surface, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_typed_insights_eval ON typed_research_insights(campaign_id, evaluation_contract_hash)",
            """CREATE TABLE IF NOT EXISTS typed_research_insight_evidence (
                insight_id TEXT NOT NULL REFERENCES typed_research_insights(insight_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                source_kind TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                PRIMARY KEY(insight_id, ordinal),
                UNIQUE(insight_id, source_kind, source_id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_typed_insight_evidence_source ON typed_research_insight_evidence(source_kind, source_id)",
            "CREATE INDEX IF NOT EXISTS idx_typed_insight_evidence_experiment ON typed_research_insight_evidence(experiment_id)",
        )
        for statement in statements:
            connection.execute(statement)


    @staticmethod
    def _create_strategy_checkpoint_schema(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS research_strategy_checkpoints (
                decision_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                decision_fingerprint TEXT NOT NULL,
                planning_fingerprint TEXT NOT NULL,
                selected_proposal_id TEXT NOT NULL,
                planned_experiment_id TEXT NOT NULL,
                registered_experiment_id TEXT REFERENCES experiments(experiment_id),
                stage TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                UNIQUE(campaign_id, decision_fingerprint),
                UNIQUE(campaign_id, planning_fingerprint),
                UNIQUE(campaign_id, planned_experiment_id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_strategy_checkpoints_campaign ON research_strategy_checkpoints(campaign_id, decision_id)",
            "CREATE INDEX IF NOT EXISTS idx_strategy_checkpoints_stage ON research_strategy_checkpoints(campaign_id, stage)",
            "CREATE INDEX IF NOT EXISTS idx_strategy_checkpoints_experiment ON research_strategy_checkpoints(registered_experiment_id)",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _create_campaign_cycle_schema(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS research_evidence_packets (
                packet_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                cycle_fingerprint TEXT NOT NULL,
                packet_fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                UNIQUE(campaign_id, cycle_fingerprint),
                UNIQUE(campaign_id, packet_fingerprint)
            )""",
            """CREATE TABLE IF NOT EXISTS research_proposal_cohorts (
                cohort_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                cycle_fingerprint TEXT NOT NULL,
                evidence_packet_fingerprint TEXT NOT NULL,
                cohort_fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                UNIQUE(campaign_id, cycle_fingerprint),
                UNIQUE(campaign_id, cohort_fingerprint)
            )""",
            """CREATE TABLE IF NOT EXISTS research_stop_decisions (
                decision_fingerprint TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES research_campaigns(campaign_id) ON DELETE CASCADE,
                cycle_fingerprint TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                UNIQUE(campaign_id, cycle_fingerprint)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_evidence_packets_campaign ON research_evidence_packets(campaign_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_proposal_cohorts_campaign ON research_proposal_cohorts(campaign_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_stop_decisions_campaign ON research_stop_decisions(campaign_id, created_at)",
        )
        for statement in statements:
            connection.execute(statement)


    @staticmethod
    def _encode(payload: Mapping[str, Any]) -> tuple[str, str]:
        text = canonical_json(payload)
        return text, stable_sha256(payload)

    @staticmethod
    def _decode(row: sqlite3.Row, *, label: str) -> Mapping[str, Any]:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ResearchStoreIntegrityError(f"corrupt {label} JSON") from exc
        if not isinstance(payload, Mapping):
            raise ResearchStoreIntegrityError(f"{label} payload must be object")
        if row["payload_hash"] != stable_sha256(payload):
            raise ResearchStoreIntegrityError(f"{label} payload hash mismatch")
        return payload

    @staticmethod
    def _require_campaign(connection: sqlite3.Connection, campaign_id: str) -> sqlite3.Row:
        safe_id(campaign_id, "campaign_id")
        row = connection.execute(
            "SELECT campaign_id, current_contract_hash, created_at, payload_hash FROM research_campaigns WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise ResearchStoreNotFoundError(f"research campaign not found: {campaign_id}")
        expected = stable_sha256(
            {
                "campaign_id": row["campaign_id"],
                "current_contract_hash": row["current_contract_hash"],
                "created_at": row["created_at"],
            }
        )
        if row["payload_hash"] != expected:
            raise ResearchStoreIntegrityError(f"campaign projection hash mismatch: {campaign_id}")
        return row

    def create_campaign(self, contract: ResearchContractV1) -> None:
        if contract.contract_version != 1 or contract.amends_contract_hash is not None:
            raise ResearchStoreConflictError("new research campaign must start from contract_version=1")
        payload_json, payload_hash = self._encode(contract.to_dict())
        campaign_projection = {
            "campaign_id": contract.campaign_id,
            "current_contract_hash": contract.contract_hash,
            "created_at": contract.created_at,
        }
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT current_contract_hash, created_at, payload_hash FROM research_campaigns WHERE campaign_id=?",
                    (contract.campaign_id,),
                ).fetchone()
                if existing is not None:
                    self._require_campaign(connection, contract.campaign_id)
                    existing_contract = self.load_research_contract(contract.contract_hash, _connection=connection, required=False)
                    if existing_contract == contract:
                        connection.rollback()
                        return
                    raise ResearchStoreConflictError(f"campaign already exists: {contract.campaign_id}")
                connection.execute(
                    "INSERT INTO research_campaigns(campaign_id,current_contract_hash,created_at,payload_hash) VALUES(?,?,?,?)",
                    (
                        contract.campaign_id,
                        contract.contract_hash,
                        contract.created_at,
                        stable_sha256(campaign_projection),
                    ),
                )
                connection.execute(
                    """INSERT INTO research_contracts(
                        contract_hash,campaign_id,contract_version,amends_contract_hash,created_at,payload_json,payload_hash
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        contract.contract_hash,
                        contract.campaign_id,
                        contract.contract_version,
                        contract.amends_contract_hash,
                        contract.created_at,
                        payload_json,
                        payload_hash,
                    ),
                )
                state = ResearchCampaignStateV1(
                    campaign_id=contract.campaign_id,
                    status=ResearchCampaignStatusV1.CREATED,
                    generation=0,
                    updated_at=contract.created_at,
                )
                state_json, state_hash = self._encode(state.to_dict())
                connection.execute(
                    """INSERT INTO research_campaign_state(
                        campaign_id,generation,status,updated_at,payload_json,payload_hash
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        state.campaign_id,
                        state.generation,
                        state.status.value,
                        state.updated_at,
                        state_json,
                        state_hash,
                    ),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot create research campaign: {exc}") from exc

    def save_research_contract(self, contract: ResearchContractV1) -> None:
        payload_json, payload_hash = self._encode(contract.to_dict())
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._require_campaign(connection, contract.campaign_id)
                existing = connection.execute(
                    "SELECT payload_json,payload_hash FROM research_contracts WHERE contract_hash=?",
                    (contract.contract_hash,),
                ).fetchone()
                if existing is not None:
                    payload = self._decode(existing, label="research contract")
                    if ResearchContractV1.from_dict(payload) != contract:
                        raise ResearchStoreIntegrityError("research contract hash identity collision")
                    connection.rollback()
                    return
                previous = self.load_research_contract(current["current_contract_hash"], _connection=connection)
                if contract.contract_version != previous.contract_version + 1:
                    raise ResearchStoreConflictError("research contract amendment must increment version by exactly one")
                if contract.amends_contract_hash != previous.contract_hash:
                    raise ResearchStoreConflictError("research contract amendment must reference current contract hash")
                connection.execute(
                    """INSERT INTO research_contracts(
                        contract_hash,campaign_id,contract_version,amends_contract_hash,created_at,payload_json,payload_hash
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        contract.contract_hash,
                        contract.campaign_id,
                        contract.contract_version,
                        contract.amends_contract_hash,
                        contract.created_at,
                        payload_json,
                        payload_hash,
                    ),
                )
                projection = {
                    "campaign_id": contract.campaign_id,
                    "current_contract_hash": contract.contract_hash,
                    "created_at": current["created_at"],
                }
                connection.execute(
                    "UPDATE research_campaigns SET current_contract_hash=?,payload_hash=? WHERE campaign_id=?",
                    (contract.contract_hash, stable_sha256(projection), contract.campaign_id),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot save research contract: {exc}") from exc

    def load_research_contract(
        self,
        contract_hash: str,
        *,
        _connection: sqlite3.Connection | None = None,
        required: bool = True,
    ) -> ResearchContractV1 | None:
        def load(connection: sqlite3.Connection) -> ResearchContractV1 | None:
            row = connection.execute(
                "SELECT payload_json,payload_hash FROM research_contracts WHERE contract_hash=?", (contract_hash,)
            ).fetchone()
            if row is None:
                if required:
                    raise ResearchStoreNotFoundError(f"research contract not found: {contract_hash}")
                return None
            try:
                return ResearchContractV1.from_dict(self._decode(row, label="research contract"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid research contract {contract_hash}: {exc}") from exc

        if _connection is not None:
            return load(_connection)
        with self._connect() as connection:
            return load(connection)

    def current_research_contract(self, campaign_id: str) -> ResearchContractV1:
        with self._connect() as connection:
            campaign = self._require_campaign(connection, campaign_id)
            result = self.load_research_contract(campaign["current_contract_hash"], _connection=connection)
            assert result is not None
            return result

    def load_campaign_state(self, campaign_id: str) -> ResearchCampaignStateV1:
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            row = connection.execute(
                "SELECT generation,status,updated_at,payload_json,payload_hash FROM research_campaign_state WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
            if row is None:
                raise ResearchStoreIntegrityError(f"campaign runtime state missing: {campaign_id}")
            try:
                state = ResearchCampaignStateV1.from_dict(self._decode(row, label="research campaign state"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid campaign runtime state {campaign_id}: {exc}") from exc
            if state.campaign_id != campaign_id:
                raise ResearchStoreIntegrityError("campaign runtime state campaign_id mismatch")
            if state.generation != int(row["generation"]) or state.status.value != row["status"] or state.updated_at != row["updated_at"]:
                raise ResearchStoreIntegrityError("campaign runtime state projection mismatch")
            return state

    def save_campaign_state(self, state: ResearchCampaignStateV1, *, expected_generation: int) -> None:
        if not isinstance(state, ResearchCampaignStateV1):
            raise ResearchStoreConflictError("state must be ResearchCampaignStateV1")
        if not isinstance(expected_generation, int) or isinstance(expected_generation, bool) or expected_generation < 0:
            raise ValueError("expected_generation must be integer >= 0")
        payload_json, payload_hash = self._encode(state.to_dict())
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_campaign(connection, state.campaign_id)
                row = connection.execute(
                    "SELECT generation,payload_json,payload_hash FROM research_campaign_state WHERE campaign_id=?",
                    (state.campaign_id,),
                ).fetchone()
                if row is None:
                    raise ResearchStoreIntegrityError(f"campaign runtime state missing: {state.campaign_id}")
                try:
                    current = ResearchCampaignStateV1.from_dict(self._decode(row, label="research campaign state"))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ResearchStoreIntegrityError(f"invalid campaign runtime state {state.campaign_id}: {exc}") from exc
                if current.generation != expected_generation:
                    raise ResearchStoreConflictError(
                        f"campaign state generation conflict expected={expected_generation} actual={current.generation}"
                    )
                if state.generation != expected_generation + 1:
                    raise ResearchStoreConflictError("campaign state generation must increment by exactly one")
                if state.campaign_id != current.campaign_id:
                    raise ResearchStoreConflictError("campaign state identity is immutable")
                if current.is_terminal:
                    raise ResearchStoreConflictError("terminal campaign state is immutable")
                for label, experiment_id in (
                    ("champion_experiment_id", state.champion_experiment_id),
                    ("last_experiment_id", state.last_experiment_id),
                ):
                    if experiment_id is None:
                        continue
                    experiment_row = connection.execute(
                        "SELECT campaign_id FROM experiments WHERE experiment_id=?", (experiment_id,)
                    ).fetchone()
                    if experiment_row is None or experiment_row["campaign_id"] != state.campaign_id:
                        raise ResearchStoreConflictError(f"campaign state {label} references missing experiment: {experiment_id}")
                current_strategy_ref = current.pending_strategy_decision_fingerprint
                next_strategy_ref = state.pending_strategy_decision_fingerprint
                if (
                    current_strategy_ref is not None
                    and next_strategy_ref is not None
                    and next_strategy_ref != current_strategy_ref
                ):
                    raise ResearchStoreConflictError(
                        "pending strategy decision fingerprint cannot be replaced before clear"
                    )
                allowed = current.transition(
                    state.status,
                    updated_at=state.updated_at,
                    champion_experiment_id=state.champion_experiment_id,
                    last_experiment_id=state.last_experiment_id,
                    reason=state.reason,
                    pending_strategy_decision_fingerprint=(
                        next_strategy_ref
                        if current_strategy_ref is None
                        else None
                    ),
                    clear_pending_strategy_decision=(
                        current_strategy_ref is not None
                        and next_strategy_ref is None
                    ),
                )
                if allowed != state:
                    raise ResearchStoreConflictError("campaign state is not a valid deterministic transition")
                updated = connection.execute(
                    """UPDATE research_campaign_state SET generation=?,status=?,updated_at=?,payload_json=?,payload_hash=?
                       WHERE campaign_id=? AND generation=?""",
                    (
                        state.generation,
                        state.status.value,
                        state.updated_at,
                        payload_json,
                        payload_hash,
                        state.campaign_id,
                        expected_generation,
                    ),
                )
                if updated.rowcount != 1:
                    raise ResearchStoreConflictError("campaign state compare-and-swap failed")
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot save campaign runtime state: {exc}") from exc

    def save_evaluation_contract(self, contract: EvaluationContractV1) -> None:
        payload_json, payload_hash = self._encode(contract.to_dict())
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT payload_json,payload_hash FROM evaluation_contracts WHERE contract_hash=?",
                    (contract.contract_hash,),
                ).fetchone()
                if existing is not None:
                    try:
                        loaded = EvaluationContractV1.from_dict(self._decode(existing, label="evaluation contract"))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ResearchStoreIntegrityError(f"invalid evaluation contract: {exc}") from exc
                    if loaded != contract:
                        raise ResearchStoreIntegrityError("evaluation contract hash identity collision")
                    connection.rollback()
                    return
                connection.execute(
                    """INSERT INTO evaluation_contracts(
                        contract_hash,metric_name,direction,dataset_fingerprint,split_fingerprint,payload_json,payload_hash
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        contract.contract_hash,
                        contract.metric_name,
                        contract.direction.value,
                        contract.dataset_fingerprint,
                        contract.split_fingerprint,
                        payload_json,
                        payload_hash,
                    ),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot save evaluation contract: {exc}") from exc

    def load_evaluation_contract(self, contract_hash: str) -> EvaluationContractV1:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,payload_hash FROM evaluation_contracts WHERE contract_hash=?", (contract_hash,)
            ).fetchone()
            if row is None:
                raise ResearchStoreNotFoundError(f"evaluation contract not found: {contract_hash}")
            try:
                return EvaluationContractV1.from_dict(self._decode(row, label="evaluation contract"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid evaluation contract {contract_hash}: {exc}") from exc

    def save_hypothesis(self, hypothesis: HypothesisV1) -> None:
        payload_json, payload_hash = self._encode(hypothesis.to_dict())
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_campaign(connection, hypothesis.campaign_id)
                for parent in hypothesis.parent_hypothesis_ids:
                    parent_row = connection.execute(
                        "SELECT campaign_id FROM hypotheses WHERE hypothesis_id=?", (parent,)
                    ).fetchone()
                    if parent_row is None or parent_row["campaign_id"] != hypothesis.campaign_id:
                        raise ResearchStoreConflictError(f"hypothesis parent is missing or cross-campaign: {parent}")
                existing = connection.execute(
                    "SELECT payload_json,payload_hash FROM hypotheses WHERE hypothesis_id=?", (hypothesis.hypothesis_id,)
                ).fetchone()
                if existing is not None:
                    try:
                        loaded = HypothesisV1.from_dict(self._decode(existing, label="hypothesis"))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ResearchStoreIntegrityError(f"invalid hypothesis: {exc}") from exc
                    if loaded != hypothesis:
                        raise ResearchStoreConflictError("hypothesis_id is immutable")
                    connection.rollback()
                    return
                connection.execute(
                    "INSERT INTO hypotheses(hypothesis_id,campaign_id,status,created_at,payload_json,payload_hash) VALUES(?,?,?,?,?,?)",
                    (
                        hypothesis.hypothesis_id,
                        hypothesis.campaign_id,
                        hypothesis.status.value,
                        hypothesis.created_at,
                        payload_json,
                        payload_hash,
                    ),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot save hypothesis: {exc}") from exc

    def load_hypothesis(self, hypothesis_id: str) -> HypothesisV1:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,payload_hash FROM hypotheses WHERE hypothesis_id=?", (hypothesis_id,)
            ).fetchone()
            if row is None:
                raise ResearchStoreNotFoundError(f"hypothesis not found: {hypothesis_id}")
            try:
                return HypothesisV1.from_dict(self._decode(row, label="hypothesis"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid hypothesis {hypothesis_id}: {exc}") from exc

    def list_hypotheses(
        self,
        campaign_id: str,
        *,
        statuses: Iterable[HypothesisStatusV1] | None = None,
        limit: int = 1000,
    ) -> tuple[HypothesisV1, ...]:
        safe_id(campaign_id, "campaign_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be integer in [1,100000]")
        clauses = ["campaign_id=?"]
        params: list[Any] = [campaign_id]
        statuses_tuple = tuple(statuses or ())
        if statuses_tuple:
            if any(not isinstance(item, HypothesisStatusV1) for item in statuses_tuple):
                raise ValueError("statuses must contain HypothesisStatusV1")
            clauses.append(f"status IN ({','.join('?' for _ in statuses_tuple)})")
            params.extend(item.value for item in statuses_tuple)
        params.append(limit)
        query = (
            "SELECT payload_json,payload_hash FROM hypotheses WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at,hypothesis_id LIMIT ?"
        )
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            rows = connection.execute(query, tuple(params)).fetchall()
            result: list[HypothesisV1] = []
            for row in rows:
                try:
                    result.append(HypothesisV1.from_dict(self._decode(row, label="hypothesis")))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ResearchStoreIntegrityError(f"invalid hypothesis: {exc}") from exc
            return tuple(result)

    def register_experiment(self, spec: ExperimentSpecV1) -> ExperimentRegistrationV1:
        return self.register_experiments((spec,))[0]

    def register_experiments(self, specs: Sequence[ExperimentSpecV1]) -> tuple[ExperimentRegistrationV1, ...]:
        if not specs:
            return ()
        registrations: list[ExperimentRegistrationV1] = []
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for spec in specs:
                    self._require_campaign(connection, spec.campaign_id)
                    hypothesis = connection.execute(
                        "SELECT campaign_id FROM hypotheses WHERE hypothesis_id=?", (spec.hypothesis_id,)
                    ).fetchone()
                    if hypothesis is None or hypothesis["campaign_id"] != spec.campaign_id:
                        raise ResearchStoreConflictError("experiment hypothesis is missing or cross-campaign")
                    evaluator = connection.execute(
                        "SELECT contract_hash,dataset_fingerprint FROM evaluation_contracts WHERE contract_hash=?",
                        (spec.evaluation_contract_hash,),
                    ).fetchone()
                    if evaluator is None:
                        raise ResearchStoreConflictError("experiment evaluation contract is not registered")
                    if evaluator["dataset_fingerprint"] != spec.dataset_fingerprint:
                        raise ResearchStoreConflictError("experiment dataset fingerprint disagrees with evaluation contract")
                    existing_id = connection.execute(
                        "SELECT experiment_id,payload_json,payload_hash FROM experiments WHERE experiment_id=?",
                        (spec.experiment_id,),
                    ).fetchone()
                    if existing_id is not None:
                        try:
                            existing_spec = ExperimentSpecV1.from_dict(self._decode(existing_id, label="experiment spec"))
                        except (KeyError, TypeError, ValueError) as exc:
                            raise ResearchStoreIntegrityError(f"invalid experiment spec: {exc}") from exc
                        if existing_spec != spec:
                            raise ResearchStoreConflictError("experiment_id already exists with different immutable spec")
                        registrations.append(
                            ExperimentRegistrationV1(spec.experiment_id, spec.experiment_id, spec.fingerprint, False, False)
                        )
                        continue
                    duplicate = connection.execute(
                        "SELECT experiment_id FROM experiments WHERE campaign_id=? AND fingerprint=?",
                        (spec.campaign_id, spec.fingerprint),
                    ).fetchone()
                    if duplicate is not None:
                        registrations.append(
                            ExperimentRegistrationV1(
                                spec.experiment_id, duplicate["experiment_id"], spec.fingerprint, False, True
                            )
                        )
                        continue
                    for parent in spec.parent_experiment_ids:
                        parent_row = connection.execute(
                            "SELECT campaign_id FROM experiments WHERE experiment_id=?", (parent,)
                        ).fetchone()
                        if parent_row is None or parent_row["campaign_id"] != spec.campaign_id:
                            raise ResearchStoreConflictError(f"experiment parent is missing or cross-campaign: {parent}")
                    payload_json, payload_hash = self._encode(spec.to_dict())
                    connection.execute(
                        """INSERT INTO experiments(
                            experiment_id,campaign_id,hypothesis_id,fingerprint,strategy_id,model_id,provider,model_version,
                            evaluation_contract_hash,dataset_fingerprint,seed,created_at,payload_json,payload_hash
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            spec.experiment_id,
                            spec.campaign_id,
                            spec.hypothesis_id,
                            spec.fingerprint,
                            spec.strategy_id,
                            spec.model_id,
                            spec.provider,
                            spec.model_version,
                            spec.evaluation_contract_hash,
                            spec.dataset_fingerprint,
                            spec.seed,
                            spec.created_at,
                            payload_json,
                            payload_hash,
                        ),
                    )
                    for ordinal, parent in enumerate(spec.parent_experiment_ids):
                        connection.execute(
                            "INSERT INTO experiment_relations(experiment_id,parent_experiment_id,ordinal) VALUES(?,?,?)",
                            (spec.experiment_id, parent, ordinal),
                        )
                    registrations.append(
                        ExperimentRegistrationV1(spec.experiment_id, spec.experiment_id, spec.fingerprint, True, False)
                    )
                connection.commit()
                return tuple(registrations)
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResearchStoreConflictError(f"experiment registration integrity conflict: {exc}") from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot register experiments: {exc}") from exc

    def register_hypothesis_experiment(
        self,
        hypothesis: HypothesisV1,
        spec: ExperimentSpecV1,
    ) -> ExperimentRegistrationV1:
        """Atomically register one adaptive hypothesis and its first experiment.

        R7.6 adaptive fallback must not leave a durable generated hypothesis without
        the experiment that made it actionable if the process crashes between two
        separate store calls.  This method reuses the existing SQLite v5 tables and
        adds no second proposal persistence surface.
        """
        if not isinstance(hypothesis, HypothesisV1):
            raise TypeError("hypothesis must be HypothesisV1")
        if not isinstance(spec, ExperimentSpecV1):
            raise TypeError("spec must be ExperimentSpecV1")
        if hypothesis.campaign_id != spec.campaign_id:
            raise ResearchStoreConflictError("adaptive hypothesis/experiment campaign_id mismatch")
        if hypothesis.hypothesis_id != spec.hypothesis_id:
            raise ResearchStoreConflictError("adaptive experiment must reference the registered hypothesis")

        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_campaign(connection, hypothesis.campaign_id)

                for parent in hypothesis.parent_hypothesis_ids:
                    parent_row = connection.execute(
                        "SELECT campaign_id FROM hypotheses WHERE hypothesis_id=?", (parent,)
                    ).fetchone()
                    if parent_row is None or parent_row["campaign_id"] != hypothesis.campaign_id:
                        raise ResearchStoreConflictError(f"hypothesis parent is missing or cross-campaign: {parent}")

                hypothesis_row = connection.execute(
                    "SELECT payload_json,payload_hash FROM hypotheses WHERE hypothesis_id=?",
                    (hypothesis.hypothesis_id,),
                ).fetchone()
                if hypothesis_row is not None:
                    try:
                        loaded = HypothesisV1.from_dict(self._decode(hypothesis_row, label="hypothesis"))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ResearchStoreIntegrityError(f"invalid hypothesis: {exc}") from exc
                    if loaded != hypothesis:
                        raise ResearchStoreConflictError("hypothesis_id is immutable")
                else:
                    payload_json, payload_hash = self._encode(hypothesis.to_dict())
                    connection.execute(
                        "INSERT INTO hypotheses(hypothesis_id,campaign_id,status,created_at,payload_json,payload_hash) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            hypothesis.hypothesis_id,
                            hypothesis.campaign_id,
                            hypothesis.status.value,
                            hypothesis.created_at,
                            payload_json,
                            payload_hash,
                        ),
                    )

                evaluator = connection.execute(
                    "SELECT contract_hash,dataset_fingerprint FROM evaluation_contracts WHERE contract_hash=?",
                    (spec.evaluation_contract_hash,),
                ).fetchone()
                if evaluator is None:
                    raise ResearchStoreConflictError("experiment evaluation contract is not registered")
                if evaluator["dataset_fingerprint"] != spec.dataset_fingerprint:
                    raise ResearchStoreConflictError("experiment dataset fingerprint disagrees with evaluation contract")

                existing_id = connection.execute(
                    "SELECT experiment_id,payload_json,payload_hash FROM experiments WHERE experiment_id=?",
                    (spec.experiment_id,),
                ).fetchone()
                if existing_id is not None:
                    try:
                        existing_spec = ExperimentSpecV1.from_dict(self._decode(existing_id, label="experiment spec"))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ResearchStoreIntegrityError(f"invalid experiment spec: {exc}") from exc
                    if existing_spec != spec:
                        raise ResearchStoreConflictError("experiment_id already exists with different immutable spec")
                    connection.commit()
                    return ExperimentRegistrationV1(
                        spec.experiment_id, spec.experiment_id, spec.fingerprint, False, False
                    )

                duplicate = connection.execute(
                    "SELECT experiment_id,hypothesis_id FROM experiments WHERE campaign_id=? AND fingerprint=?",
                    (spec.campaign_id, spec.fingerprint),
                ).fetchone()
                if duplicate is not None:
                    if duplicate["hypothesis_id"] != hypothesis.hypothesis_id:
                        raise ResearchStoreConflictError(
                            "adaptive experiment duplicates logical work owned by another hypothesis"
                        )
                    connection.commit()
                    return ExperimentRegistrationV1(
                        spec.experiment_id, duplicate["experiment_id"], spec.fingerprint, False, True
                    )

                for parent in spec.parent_experiment_ids:
                    parent_row = connection.execute(
                        "SELECT campaign_id FROM experiments WHERE experiment_id=?", (parent,)
                    ).fetchone()
                    if parent_row is None or parent_row["campaign_id"] != spec.campaign_id:
                        raise ResearchStoreConflictError(f"experiment parent is missing or cross-campaign: {parent}")

                payload_json, payload_hash = self._encode(spec.to_dict())
                connection.execute(
                    """INSERT INTO experiments(
                        experiment_id,campaign_id,hypothesis_id,fingerprint,strategy_id,model_id,provider,model_version,
                        evaluation_contract_hash,dataset_fingerprint,seed,created_at,payload_json,payload_hash
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        spec.experiment_id,
                        spec.campaign_id,
                        spec.hypothesis_id,
                        spec.fingerprint,
                        spec.strategy_id,
                        spec.model_id,
                        spec.provider,
                        spec.model_version,
                        spec.evaluation_contract_hash,
                        spec.dataset_fingerprint,
                        spec.seed,
                        spec.created_at,
                        payload_json,
                        payload_hash,
                    ),
                )
                for ordinal, parent in enumerate(spec.parent_experiment_ids):
                    connection.execute(
                        "INSERT INTO experiment_relations(experiment_id,parent_experiment_id,ordinal) VALUES(?,?,?)",
                        (spec.experiment_id, parent, ordinal),
                    )
                connection.commit()
                return ExperimentRegistrationV1(
                    spec.experiment_id, spec.experiment_id, spec.fingerprint, True, False
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResearchStoreConflictError(f"adaptive hypothesis/experiment registration conflict: {exc}") from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot register adaptive hypothesis/experiment: {exc}") from exc


    def _decode_strategy_checkpoint(self, row: sqlite3.Row) -> Any:
        from .strategy_durability import ResearchStrategyCheckpointV1

        try:
            checkpoint = ResearchStrategyCheckpointV1.from_dict(
                self._decode(row, label="research strategy checkpoint")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchStoreIntegrityError(
                f"invalid research strategy checkpoint: {exc}"
            ) from exc

        decision = checkpoint.selection.decision
        if decision.decision_id != row["decision_id"]:
            raise ResearchStoreIntegrityError("strategy checkpoint decision_id projection mismatch")
        if decision.campaign_id != row["campaign_id"]:
            raise ResearchStoreIntegrityError("strategy checkpoint campaign_id projection mismatch")
        if checkpoint.decision_fingerprint != row["decision_fingerprint"]:
            raise ResearchStoreIntegrityError("strategy checkpoint decision fingerprint projection mismatch")
        if checkpoint.planning_fingerprint != row["planning_fingerprint"]:
            raise ResearchStoreIntegrityError("strategy checkpoint planning fingerprint projection mismatch")
        if checkpoint.selected_proposal.proposal_id != row["selected_proposal_id"]:
            raise ResearchStoreIntegrityError("strategy checkpoint selected proposal projection mismatch")
        if checkpoint.planned_experiment.experiment_id != row["planned_experiment_id"]:
            raise ResearchStoreIntegrityError("strategy checkpoint planned experiment projection mismatch")
        if checkpoint.registered_experiment_id != row["registered_experiment_id"]:
            raise ResearchStoreIntegrityError("strategy checkpoint registered experiment projection mismatch")
        if checkpoint.stage.value != row["stage"]:
            raise ResearchStoreIntegrityError("strategy checkpoint stage projection mismatch")
        return checkpoint

    def save_strategy_checkpoint(self, checkpoint: Any) -> None:
        from .strategy_durability import (
            ResearchStrategyCheckpointStageV1,
            ResearchStrategyCheckpointV1,
        )

        if not isinstance(checkpoint, ResearchStrategyCheckpointV1):
            raise TypeError("checkpoint must be ResearchStrategyCheckpointV1")
        if checkpoint.stage is not ResearchStrategyCheckpointStageV1.DECISION_PERSISTED:
            raise ResearchStoreConflictError(
                "initial strategy checkpoint must be DECISION_PERSISTED"
            )

        payload_json, payload_hash = self._encode(checkpoint.to_dict())
        decision = checkpoint.selection.decision
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_campaign(connection, decision.campaign_id)
                existing = connection.execute(
                    """SELECT decision_id,campaign_id,decision_fingerprint,planning_fingerprint,
                              selected_proposal_id,planned_experiment_id,registered_experiment_id,
                              stage,payload_json,payload_hash
                       FROM research_strategy_checkpoints
                       WHERE decision_id=? OR (campaign_id=? AND decision_fingerprint=?)""",
                    (
                        decision.decision_id,
                        decision.campaign_id,
                        checkpoint.decision_fingerprint,
                    ),
                ).fetchall()
                if existing:
                    if len(existing) != 1:
                        raise ResearchStoreIntegrityError(
                            "strategy checkpoint identity resolves to multiple rows"
                        )
                    loaded = self._decode_strategy_checkpoint(existing[0])
                    if loaded.planning_fingerprint != checkpoint.planning_fingerprint:
                        raise ResearchStoreConflictError(
                            "strategy decision identity already exists with different immutable plan"
                        )
                    connection.rollback()
                    return

                connection.execute(
                    """INSERT INTO research_strategy_checkpoints(
                        decision_id,campaign_id,decision_fingerprint,planning_fingerprint,
                        selected_proposal_id,planned_experiment_id,registered_experiment_id,
                        stage,payload_json,payload_hash
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        decision.decision_id,
                        decision.campaign_id,
                        checkpoint.decision_fingerprint,
                        checkpoint.planning_fingerprint,
                        checkpoint.selected_proposal.proposal_id,
                        checkpoint.planned_experiment.experiment_id,
                        None,
                        checkpoint.stage.value,
                        payload_json,
                        payload_hash,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResearchStoreConflictError(
                    f"strategy checkpoint registration integrity conflict: {exc}"
                ) from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(
                    f"cannot save research strategy checkpoint: {exc}"
                ) from exc

    def list_strategy_checkpoints(
        self,
        campaign_id: str,
        *,
        limit: int = 1000,
    ) -> tuple[Any, ...]:
        # Deterministic durable R8 checkpoint enumeration for research stopping restart.
        safe_id(campaign_id, "campaign_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be integer in [1,100000]")
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            rows = connection.execute(
                "SELECT decision_id,campaign_id,decision_fingerprint,planning_fingerprint, "
                "selected_proposal_id,planned_experiment_id,registered_experiment_id, "
                "stage,payload_json,payload_hash "
                "FROM research_strategy_checkpoints "
                "WHERE campaign_id=? ORDER BY decision_id LIMIT ?",
                (campaign_id, limit),
            ).fetchall()
            return tuple(self._decode_strategy_checkpoint(row) for row in rows)

    def list_strategy_decision_fingerprints(
        self,
        campaign_id: str,
        *,
        limit: int = 1000,
    ) -> tuple[str, ...]:
        """Return the ordered durable R8 decision identity projection.

        This is a read-only hot-path projection.  Full checkpoint decoding remains
        authoritative for recovery/active-checkpoint handling.
        """
        safe_id(campaign_id, "campaign_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be integer in [1,100000]")
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            rows = connection.execute(
                "SELECT decision_fingerprint FROM research_strategy_checkpoints "
                "WHERE campaign_id=? ORDER BY decision_id LIMIT ?",
                (campaign_id, limit),
            ).fetchall()

        fingerprints: list[str] = []
        for row in rows:
            try:
                fingerprints.append(
                    sha256_hex(str(row["decision_fingerprint"]), "decision_fingerprint")
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(
                    f"invalid research strategy decision fingerprint projection: {exc}"
                ) from exc
        return tuple(fingerprints)

    def list_strategy_selected_proposals(
        self,
        campaign_id: str,
        *,
        limit: int = 1000,
    ) -> tuple[Any, ...]:
        """Return only selected proposals from durable R8 checkpoints.

        The checkpoint payload hash and selected-proposal projection are validated,
        but unrelated nested checkpoint objects are not reconstructed.  Full
        checkpoint decoding remains in ``list_strategy_checkpoints``.
        """
        from .strategy_contracts import ResearchProposalV1

        safe_id(campaign_id, "campaign_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be integer in [1,100000]")
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            rows = connection.execute(
                "SELECT campaign_id,selected_proposal_id,payload_json,payload_hash "
                "FROM research_strategy_checkpoints "
                "WHERE campaign_id=? ORDER BY decision_id LIMIT ?",
                (campaign_id, limit),
            ).fetchall()

        proposals: list[ResearchProposalV1] = []
        for row in rows:
            try:
                payload = self._decode(row, label="research strategy checkpoint projection")
                if payload.get("schema_version") != 1:
                    raise ValueError("ResearchStrategyCheckpointV1 schema_version must be 1")
                proposal = ResearchProposalV1.from_dict(payload["selected_proposal"])
                row_campaign_id = safe_id(str(row["campaign_id"]), "checkpoint campaign_id")
                selected_proposal_id = safe_id(
                    str(row["selected_proposal_id"]),
                    "selected_proposal_id",
                )
                if row_campaign_id != campaign_id or proposal.campaign_id != campaign_id:
                    raise ValueError("selected proposal checkpoint campaign mismatch")
                if proposal.proposal_id != selected_proposal_id:
                    raise ValueError("selected proposal checkpoint projection mismatch")
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(
                    f"invalid research strategy selected-proposal projection: {exc}"
                ) from exc
            proposals.append(proposal)
        return tuple(proposals)

    def list_strategy_recovery_checkpoints(
        self,
        campaign_id: str,
        *,
        limit: int = 1000,
    ) -> tuple[Any, ...]:
        """Return only R8 checkpoints that can still own unfinished work.

        Historical EXPERIMENT_PLANNED checkpoints whose registered experiment is
        already terminal are excluded by the durable result projection. Candidate
        rows are still decoded through the full ResearchStrategyCheckpointV1
        integrity path before they can influence restart reconciliation.
        """
        from .strategy_durability import ResearchStrategyCheckpointStageV1

        safe_id(campaign_id, "campaign_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be integer in [1,100000]")

        terminal_statuses = tuple(
            item.value for item in sorted(_TERMINAL_STATUSES, key=lambda item: item.value)
        )
        terminal_placeholders = ",".join("?" for _ in terminal_statuses)
        persisted = ResearchStrategyCheckpointStageV1.DECISION_PERSISTED.value
        planned = ResearchStrategyCheckpointStageV1.EXPERIMENT_PLANNED.value

        sql = (
            "SELECT c.decision_id,c.campaign_id,c.decision_fingerprint,c.planning_fingerprint, "
            "c.selected_proposal_id,c.planned_experiment_id,c.registered_experiment_id, "
            "c.stage,c.payload_json,c.payload_hash "
            "FROM research_strategy_checkpoints c "
            "LEFT JOIN experiment_results r ON r.experiment_id=c.registered_experiment_id "
            "WHERE c.campaign_id=? AND ("
            "c.stage=? OR "
            f"(c.stage=? AND (r.status IS NULL OR r.status NOT IN ({terminal_placeholders}))) OR "
            "c.stage NOT IN (?,?)"
            ") ORDER BY c.decision_id LIMIT ?"
        )
        args: list[Any] = [campaign_id, persisted, planned]
        args.extend(terminal_statuses)
        args.extend((persisted, planned, limit))

        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            rows = connection.execute(sql, tuple(args)).fetchall()
            return tuple(self._decode_strategy_checkpoint(row) for row in rows)

    def load_strategy_checkpoint_by_fingerprint(
        self,
        campaign_id: str,
        decision_fingerprint: str,
    ) -> Any:
        safe_id(campaign_id, "campaign_id")
        decision_fingerprint = sha256_hex(
            decision_fingerprint,
            "decision_fingerprint",
        )
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            row = connection.execute(
                """SELECT decision_id,campaign_id,decision_fingerprint,planning_fingerprint,
                          selected_proposal_id,planned_experiment_id,registered_experiment_id,
                          stage,payload_json,payload_hash
                   FROM research_strategy_checkpoints
                   WHERE campaign_id=? AND decision_fingerprint=?""",
                (campaign_id, decision_fingerprint),
            ).fetchone()
            if row is None:
                raise ResearchStoreNotFoundError(
                    f"research strategy checkpoint not found: {decision_fingerprint}"
                )
            return self._decode_strategy_checkpoint(row)

    def mark_strategy_checkpoint_planned(self, checkpoint: Any) -> None:
        from .strategy_durability import (
            ResearchStrategyCheckpointStageV1,
            ResearchStrategyCheckpointV1,
        )

        if not isinstance(checkpoint, ResearchStrategyCheckpointV1):
            raise TypeError("checkpoint must be ResearchStrategyCheckpointV1")
        if checkpoint.stage is not ResearchStrategyCheckpointStageV1.EXPERIMENT_PLANNED:
            raise ResearchStoreConflictError(
                "strategy checkpoint update must be EXPERIMENT_PLANNED"
            )
        if checkpoint.registered_experiment_id is None:
            raise ResearchStoreConflictError(
                "planned strategy checkpoint requires registered experiment"
            )

        payload_json, payload_hash = self._encode(checkpoint.to_dict())
        decision = checkpoint.selection.decision
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_campaign(connection, decision.campaign_id)
                row = connection.execute(
                    """SELECT decision_id,campaign_id,decision_fingerprint,planning_fingerprint,
                              selected_proposal_id,planned_experiment_id,registered_experiment_id,
                              stage,payload_json,payload_hash
                       FROM research_strategy_checkpoints
                       WHERE campaign_id=? AND decision_fingerprint=?""",
                    (decision.campaign_id, checkpoint.decision_fingerprint),
                ).fetchone()
                if row is None:
                    raise ResearchStoreNotFoundError(
                        f"research strategy checkpoint not found: {checkpoint.decision_fingerprint}"
                    )
                current = self._decode_strategy_checkpoint(row)
                if current.planning_fingerprint != checkpoint.planning_fingerprint:
                    raise ResearchStoreConflictError(
                        "strategy checkpoint immutable planning fingerprint changed"
                    )
                if current.stage is ResearchStrategyCheckpointStageV1.EXPERIMENT_PLANNED:
                    if current != checkpoint:
                        raise ResearchStoreConflictError(
                            "planned strategy checkpoint is immutable"
                        )
                    connection.rollback()
                    return
                if current.stage is not ResearchStrategyCheckpointStageV1.DECISION_PERSISTED:
                    raise ResearchStoreConflictError(
                        "invalid strategy checkpoint stage transition"
                    )

                experiment_row = connection.execute(
                    "SELECT payload_json,payload_hash FROM experiments WHERE experiment_id=?",
                    (checkpoint.registered_experiment_id,),
                ).fetchone()
                if experiment_row is None:
                    raise ResearchStoreConflictError(
                        "registered strategy experiment is missing"
                    )
                try:
                    experiment = ExperimentSpecV1.from_dict(
                        self._decode(experiment_row, label="experiment spec")
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ResearchStoreIntegrityError(
                        f"invalid registered strategy experiment: {exc}"
                    ) from exc
                if experiment != checkpoint.planned_experiment:
                    raise ResearchStoreConflictError(
                        "registered strategy experiment differs from exact planned spec"
                    )

                updated = connection.execute(
                    """UPDATE research_strategy_checkpoints
                       SET registered_experiment_id=?,stage=?,payload_json=?,payload_hash=?
                       WHERE decision_id=? AND stage=?""",
                    (
                        checkpoint.registered_experiment_id,
                        checkpoint.stage.value,
                        payload_json,
                        payload_hash,
                        decision.decision_id,
                        ResearchStrategyCheckpointStageV1.DECISION_PERSISTED.value,
                    ),
                )
                if updated.rowcount != 1:
                    raise ResearchStoreConflictError(
                        "strategy checkpoint stage compare-and-swap failed"
                    )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResearchStoreConflictError(
                    f"strategy checkpoint planning integrity conflict: {exc}"
                ) from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(
                    f"cannot mark research strategy checkpoint planned: {exc}"
                ) from exc

    def save_evidence_packet(self, packet: ResearchEvidencePacketV1, *, cycle_fingerprint: str) -> None:
        if not isinstance(packet, ResearchEvidencePacketV1):
            raise TypeError("packet must be ResearchEvidencePacketV1")
        self._save_campaign_cycle_record("research_evidence_packets", "packet_id", packet.packet_id, packet.campaign_id,
                                  cycle_fingerprint, "packet_fingerprint", packet.fingerprint, packet.created_at, packet.to_dict())

    def load_evidence_packet_for_cycle(self, campaign_id: str, cycle_fingerprint: str) -> ResearchEvidencePacketV1 | None:
        payload = self._load_campaign_cycle_payload("research_evidence_packets", campaign_id, cycle_fingerprint, "evidence packet")
        return None if payload is None else ResearchEvidencePacketV1.from_dict(payload)

    def save_proposal_cohort(self, cohort: Any) -> None:
        from .model_proposals import ResearchProposalCohortRecordV1

        if not isinstance(cohort, ResearchProposalCohortRecordV1):
            raise TypeError("cohort must be ResearchProposalCohortRecordV1")
        self._save_campaign_cycle_record("research_proposal_cohorts", "cohort_id", cohort.cohort_id, cohort.campaign_id,
                                  cohort.cycle_fingerprint, "cohort_fingerprint", cohort.fingerprint, cohort.created_at, cohort.to_dict(),
                                  extra=("evidence_packet_fingerprint", cohort.evidence_packet_fingerprint))

    def load_proposal_cohort_for_cycle(self, campaign_id: str, cycle_fingerprint: str) -> Any:
        from .model_proposals import ResearchProposalCohortRecordV1

        payload = self._load_campaign_cycle_payload("research_proposal_cohorts", campaign_id, cycle_fingerprint, "proposal cohort")
        return None if payload is None else ResearchProposalCohortRecordV1.from_dict(payload)

    def save_stop_decision(self, decision: ResearchStopDecisionV1, *, cycle_fingerprint: str) -> None:
        if not isinstance(decision, ResearchStopDecisionV1):
            raise TypeError("decision must be ResearchStopDecisionV1")
        self._save_campaign_cycle_record("research_stop_decisions", "decision_fingerprint", decision.fingerprint, decision.campaign_id,
                                  cycle_fingerprint, "reason", decision.reason.value, decision.created_at, decision.to_dict())

    def load_stop_decision_for_cycle(self, campaign_id: str, cycle_fingerprint: str) -> ResearchStopDecisionV1 | None:
        payload = self._load_campaign_cycle_payload("research_stop_decisions", campaign_id, cycle_fingerprint, "stop decision")
        return None if payload is None else ResearchStopDecisionV1.from_dict(payload)

    def latest_terminal_stop_decision(self, campaign_id: str) -> ResearchStopDecisionV1 | None:
        safe_id(campaign_id, "campaign_id")
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            row = connection.execute("SELECT payload_json,payload_hash FROM research_stop_decisions WHERE campaign_id=? AND reason<>? ORDER BY created_at DESC LIMIT 1",
                                     (campaign_id, "CONTINUE")).fetchone()
            return None if row is None else ResearchStopDecisionV1.from_dict(self._decode(row, label="stop decision"))

    def list_evidence_packets(self, campaign_id: str) -> tuple[ResearchEvidencePacketV1, ...]:
        return tuple(ResearchEvidencePacketV1.from_dict(item) for item in
                     self._list_campaign_cycle_payloads("research_evidence_packets", campaign_id, "evidence packet"))

    def list_proposal_cohorts(self, campaign_id: str) -> tuple[Any, ...]:
        from .model_proposals import ResearchProposalCohortRecordV1
        return tuple(ResearchProposalCohortRecordV1.from_dict(item) for item in
                     self._list_campaign_cycle_payloads("research_proposal_cohorts", campaign_id, "proposal cohort"))

    def list_stop_decisions(self, campaign_id: str) -> tuple[ResearchStopDecisionV1, ...]:
        return tuple(ResearchStopDecisionV1.from_dict(item) for item in
                     self._list_campaign_cycle_payloads("research_stop_decisions", campaign_id, "stop decision"))

    def _list_campaign_cycle_payloads(self, table: str, campaign_id: str, label: str) -> tuple[Mapping[str, Any], ...]:
        if table not in {"research_evidence_packets", "research_proposal_cohorts", "research_stop_decisions"}:
            raise ValueError("unsupported Phase23 table")
        safe_id(campaign_id, "campaign_id")
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            rows = connection.execute(f"SELECT payload_json,payload_hash FROM {table} WHERE campaign_id=? ORDER BY created_at",
                                      (campaign_id,)).fetchall()
            return tuple(self._decode(row, label=label) for row in rows)

    def _load_campaign_cycle_payload(self, table: str, campaign_id: str, cycle_fingerprint: str, label: str) -> Mapping[str, Any] | None:
        if table not in {"research_evidence_packets", "research_proposal_cohorts", "research_stop_decisions"}:
            raise ValueError("unsupported Phase23 table")
        safe_id(campaign_id, "campaign_id")
        cycle_fingerprint = sha256_hex(cycle_fingerprint, "cycle_fingerprint")
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            row = connection.execute(f"SELECT payload_json,payload_hash FROM {table} WHERE campaign_id=? AND cycle_fingerprint=?",
                                     (campaign_id, cycle_fingerprint)).fetchone()
            return None if row is None else self._decode(row, label=label)

    def _save_campaign_cycle_record(self, table: str, identity_column: str, identity: str, campaign_id: str,
                             cycle_fingerprint: str, projection_column: str, projection: str, created_at: str,
                             payload: Mapping[str, Any], *, extra: tuple[str, str] | None = None) -> None:
        allowed = {
            "research_evidence_packets": ("packet_id", "packet_fingerprint"),
            "research_proposal_cohorts": ("cohort_id", "cohort_fingerprint"),
            "research_stop_decisions": ("decision_fingerprint", "reason"),
        }
        if allowed.get(table) != (identity_column, projection_column):
            raise ValueError("unsupported Phase23 record projection")
        safe_id(campaign_id, "campaign_id")
        cycle_fingerprint = sha256_hex(cycle_fingerprint, "cycle_fingerprint")
        payload_json, payload_hash = self._encode(payload)
        columns = [identity_column, "campaign_id", "cycle_fingerprint", projection_column, "created_at", "payload_json", "payload_hash"]
        values: list[Any] = [identity, campaign_id, cycle_fingerprint, projection, timestamp(created_at, "created_at"), payload_json, payload_hash]
        if extra is not None:
            columns.insert(4, extra[0])
            values.insert(4, extra[1])
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_campaign(connection, campaign_id)
                row = connection.execute(f"SELECT payload_json,payload_hash FROM {table} WHERE {identity_column}=? OR (campaign_id=? AND cycle_fingerprint=?)",
                                         (identity, campaign_id, cycle_fingerprint)).fetchone()
                if row is not None:
                    if self._decode(row, label=table) != payload:
                        raise ResearchStoreConflictError(f"{table} identity already has different immutable payload")
                    connection.rollback()
                    return
                marks = ",".join("?" for _ in columns)
                connection.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({marks})", tuple(values))
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResearchStoreConflictError(f"cannot persist {table}: {exc}") from exc

    def load_experiment_spec(self, experiment_id: str) -> ExperimentSpecV1:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,payload_hash FROM experiments WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
            if row is None:
                raise ResearchStoreNotFoundError(f"experiment not found: {experiment_id}")
            try:
                return ExperimentSpecV1.from_dict(self._decode(row, label="experiment spec"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid experiment spec {experiment_id}: {exc}") from exc

    def find_by_fingerprint(self, campaign_id: str, fingerprint: str) -> StoredExperimentV1 | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT experiment_id FROM experiments WHERE campaign_id=? AND fingerprint=?", (campaign_id, fingerprint)
            ).fetchone()
            if row is None:
                return None
            return self._load_stored_experiment(connection, row["experiment_id"])

    def register_candidate_evaluation(
        self,
        spec: CandidateEvaluationSpecV1,
    ) -> CandidateEvaluationRegistrationV1:
        if not isinstance(spec, CandidateEvaluationSpecV1):
            raise TypeError("spec must be CandidateEvaluationSpecV1")
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_campaign(connection, spec.campaign_id)
                source = self._load_stored_experiment(connection, spec.candidate.source_experiment_id)
                if source.spec.campaign_id != spec.campaign_id:
                    raise ResearchStoreConflictError("candidate source experiment is cross-campaign")
                if source.spec.fingerprint != spec.candidate.source_experiment_fingerprint:
                    raise ResearchStoreConflictError("candidate source experiment fingerprint mismatch")
                if source.spec.evaluation_contract_hash != spec.evaluation_contract_hash:
                    raise ResearchStoreConflictError("candidate source EvaluationContractHash mismatch")
                if (
                    source.result is None
                    or source.result.status is not ExperimentStatusV1.SUCCEEDED
                    or source.result.candidate_commit is None
                ):
                    raise ResearchStoreConflictError("candidate evaluation requires successful source experiment commit")
                if source.result.candidate_commit != spec.candidate.candidate_commit:
                    raise ResearchStoreConflictError("candidate commit does not match immutable source experiment result")
                evaluator = connection.execute(
                    "SELECT contract_hash FROM evaluation_contracts WHERE contract_hash=?",
                    (spec.evaluation_contract_hash,),
                ).fetchone()
                if evaluator is None:
                    raise ResearchStoreConflictError("candidate evaluation contract is not registered")

                existing_id = connection.execute(
                    "SELECT evaluation_id,payload_json,payload_hash FROM candidate_evaluations WHERE evaluation_id=?",
                    (spec.evaluation_id,),
                ).fetchone()
                if existing_id is not None:
                    try:
                        existing = CandidateEvaluationSpecV1.from_dict(
                            self._decode(existing_id, label="candidate evaluation spec")
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ResearchStoreIntegrityError(f"invalid candidate evaluation spec: {exc}") from exc
                    if existing != spec:
                        raise ResearchStoreConflictError(
                            "candidate evaluation_id already exists with different immutable spec"
                        )
                    connection.rollback()
                    return CandidateEvaluationRegistrationV1(
                        spec.evaluation_id, spec.evaluation_id, spec.fingerprint, False, False
                    )

                duplicate = connection.execute(
                    "SELECT evaluation_id FROM candidate_evaluations WHERE campaign_id=? AND fingerprint=?",
                    (spec.campaign_id, spec.fingerprint),
                ).fetchone()
                if duplicate is not None:
                    connection.rollback()
                    return CandidateEvaluationRegistrationV1(
                        spec.evaluation_id, str(duplicate["evaluation_id"]), spec.fingerprint, False, True
                    )

                payload_json, payload_hash = self._encode(spec.to_dict())
                connection.execute(
                    """INSERT INTO candidate_evaluations(
                        evaluation_id,campaign_id,source_experiment_id,source_experiment_fingerprint,
                        candidate_commit,fingerprint,proposal_kind,surface,evaluation_contract_hash,seed,
                        created_at,payload_json,payload_hash
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        spec.evaluation_id,
                        spec.campaign_id,
                        spec.candidate.source_experiment_id,
                        spec.candidate.source_experiment_fingerprint,
                        spec.candidate.candidate_commit,
                        spec.fingerprint,
                        spec.proposal_kind.value,
                        spec.surface.value,
                        spec.evaluation_contract_hash,
                        spec.seed,
                        spec.created_at,
                        payload_json,
                        payload_hash,
                    ),
                )
                connection.commit()
                return CandidateEvaluationRegistrationV1(
                    spec.evaluation_id, spec.evaluation_id, spec.fingerprint, True, False
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResearchStoreConflictError(f"candidate evaluation registration conflict: {exc}") from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot register candidate evaluation: {exc}") from exc

    def load_candidate_evaluation_spec(self, evaluation_id: str) -> CandidateEvaluationSpecV1:
        safe_id(evaluation_id, "evaluation_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,payload_hash FROM candidate_evaluations WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if row is None:
                raise ResearchStoreNotFoundError(f"candidate evaluation not found: {evaluation_id}")
            try:
                return CandidateEvaluationSpecV1.from_dict(self._decode(row, label="candidate evaluation spec"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid candidate evaluation spec {evaluation_id}: {exc}") from exc

    def find_candidate_evaluation_by_fingerprint(
        self, campaign_id: str, fingerprint: str
    ) -> StoredCandidateEvaluationV1 | None:
        safe_id(campaign_id, "campaign_id")
        sha256_hex(fingerprint, "fingerprint")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT evaluation_id FROM candidate_evaluations WHERE campaign_id=? AND fingerprint=?",
                (campaign_id, fingerprint),
            ).fetchone()
            if row is None:
                return None
            return self._load_stored_candidate_evaluation(connection, str(row["evaluation_id"]))

    @staticmethod
    def _valid_candidate_evaluation_result_transition(
        old: CandidateEvaluationStatusV1,
        new: CandidateEvaluationStatusV1,
    ) -> bool:
        if old is CandidateEvaluationStatusV1.PLANNED:
            return new is CandidateEvaluationStatusV1.RUNNING or new in {
                CandidateEvaluationStatusV1.SCORED,
                CandidateEvaluationStatusV1.FAILED,
                CandidateEvaluationStatusV1.INVALID,
                CandidateEvaluationStatusV1.CANCELLED,
            }
        if old is CandidateEvaluationStatusV1.RUNNING:
            return new in {
                CandidateEvaluationStatusV1.SCORED,
                CandidateEvaluationStatusV1.FAILED,
                CandidateEvaluationStatusV1.INVALID,
                CandidateEvaluationStatusV1.CANCELLED,
            }
        return False

    def save_candidate_evaluation_result(self, result: CandidateEvaluationResultV1) -> None:
        if not isinstance(result, CandidateEvaluationResultV1):
            raise TypeError("result must be CandidateEvaluationResultV1")
        payload_json, payload_hash = self._encode(result.to_dict())
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                spec_row = connection.execute(
                    "SELECT campaign_id FROM candidate_evaluations WHERE evaluation_id=?",
                    (result.evaluation_id,),
                ).fetchone()
                if spec_row is None:
                    raise ResearchStoreNotFoundError(f"candidate evaluation not found: {result.evaluation_id}")
                if spec_row["campaign_id"] != result.campaign_id:
                    raise ResearchStoreConflictError("candidate evaluation result campaign_id mismatch")
                current_row = connection.execute(
                    "SELECT * FROM candidate_evaluation_results WHERE evaluation_id=?",
                    (result.evaluation_id,),
                ).fetchone()
                if current_row is not None:
                    try:
                        current = CandidateEvaluationResultV1.from_dict(
                            self._decode(current_row, label="candidate evaluation result")
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ResearchStoreIntegrityError(f"invalid candidate evaluation result: {exc}") from exc
                    if current == result:
                        connection.rollback()
                        return
                    if current.is_terminal:
                        raise ResearchStoreConflictError("terminal candidate evaluation result is immutable")
                    if not self._valid_candidate_evaluation_result_transition(current.status, result.status):
                        raise ResearchStoreConflictError(
                            f"invalid candidate evaluation transition {current.status.value}->{result.status.value}"
                        )
                    connection.execute(
                        """UPDATE candidate_evaluation_results SET
                           status=?,primary_metric=?,started_at=?,ended_at=?,failure_kind=?,payload_json=?,payload_hash=?
                           WHERE evaluation_id=?""",
                        (
                            result.status.value,
                            result.primary_metric,
                            result.started_at,
                            result.ended_at,
                            None if result.failure_kind is None else result.failure_kind.value,
                            payload_json,
                            payload_hash,
                            result.evaluation_id,
                        ),
                    )
                else:
                    connection.execute(
                        """INSERT INTO candidate_evaluation_results(
                            evaluation_id,campaign_id,status,primary_metric,started_at,ended_at,failure_kind,
                            payload_json,payload_hash
                        ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            result.evaluation_id,
                            result.campaign_id,
                            result.status.value,
                            result.primary_metric,
                            result.started_at,
                            result.ended_at,
                            None if result.failure_kind is None else result.failure_kind.value,
                            payload_json,
                            payload_hash,
                        ),
                    )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResearchStoreConflictError(f"candidate evaluation result conflict: {exc}") from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot save candidate evaluation result: {exc}") from exc

    def load_candidate_evaluation_result(self, evaluation_id: str) -> CandidateEvaluationResultV1 | None:
        safe_id(evaluation_id, "evaluation_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,payload_hash FROM candidate_evaluation_results WHERE evaluation_id=?",
                (evaluation_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                return CandidateEvaluationResultV1.from_dict(
                    self._decode(row, label="candidate evaluation result")
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid candidate evaluation result {evaluation_id}: {exc}") from exc

    def _load_stored_candidate_evaluation(
        self, connection: sqlite3.Connection, evaluation_id: str
    ) -> StoredCandidateEvaluationV1:
        spec_row = connection.execute(
            "SELECT payload_json,payload_hash FROM candidate_evaluations WHERE evaluation_id=?",
            (evaluation_id,),
        ).fetchone()
        if spec_row is None:
            raise ResearchStoreNotFoundError(f"candidate evaluation not found: {evaluation_id}")
        try:
            spec = CandidateEvaluationSpecV1.from_dict(self._decode(spec_row, label="candidate evaluation spec"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchStoreIntegrityError(f"invalid candidate evaluation spec {evaluation_id}: {exc}") from exc
        result_row = connection.execute(
            "SELECT payload_json,payload_hash FROM candidate_evaluation_results WHERE evaluation_id=?",
            (evaluation_id,),
        ).fetchone()
        result = None
        if result_row is not None:
            try:
                result = CandidateEvaluationResultV1.from_dict(
                    self._decode(result_row, label="candidate evaluation result")
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid candidate evaluation result {evaluation_id}: {exc}") from exc
        return StoredCandidateEvaluationV1(spec=spec, result=result)

    def list_candidate_evaluations(
        self,
        campaign_id: str,
        *,
        surface: CandidateEvaluationSurfaceV1 | None = None,
        statuses: Sequence[CandidateEvaluationStatusV1] | None = None,
        limit: int = 1000,
    ) -> tuple[StoredCandidateEvaluationV1, ...]:
        safe_id(campaign_id, "campaign_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be integer in [1,100000]")
        clauses = ["e.campaign_id=?"]
        params: list[Any] = [campaign_id]
        if surface is not None:
            if not isinstance(surface, CandidateEvaluationSurfaceV1):
                raise ValueError("surface must be CandidateEvaluationSurfaceV1")
            clauses.append("e.surface=?")
            params.append(surface.value)
        if statuses is not None:
            if not statuses:
                return ()
            values = []
            for status in statuses:
                if not isinstance(status, CandidateEvaluationStatusV1):
                    raise ValueError("statuses must contain CandidateEvaluationStatusV1")
                values.append(status.value)
            clauses.append(f"r.status IN ({','.join('?' for _ in values)})")
            params.extend(values)
        query = (
            "SELECT e.evaluation_id FROM candidate_evaluations e "
            "LEFT JOIN candidate_evaluation_results r ON r.evaluation_id=e.evaluation_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY e.created_at,e.evaluation_id LIMIT ?"
        )
        params.append(limit)
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            rows = connection.execute(query, tuple(params)).fetchall()
            return tuple(
                self._load_stored_candidate_evaluation(connection, str(row["evaluation_id"])) for row in rows
            )

    def register_repair_proposal(self, proposal: RepairProposalV1) -> RepairRegistrationV1:
        """Atomically persist repair lineage, hypothesis, and the new repair experiment.

        One terminal GATE evaluation may create at most one repair proposal.  The
        method is restart-idempotent for an identical immutable proposal and
        rejects planner drift for an already-durable failure trigger.
        """
        if not isinstance(proposal, RepairProposalV1):
            raise TypeError("proposal must be RepairProposalV1")
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_campaign(connection, proposal.campaign_id)
                gate = self._load_stored_candidate_evaluation(
                    connection, proposal.evidence.gate_evaluation_id
                )
                try:
                    expected_evidence = GateFailureEvidenceV1.from_candidate_evaluation(gate)
                except ValueError as exc:
                    raise ResearchStoreConflictError(f"repair source is not failed GATE evidence: {exc}") from exc
                if expected_evidence != proposal.evidence:
                    raise ResearchStoreConflictError("repair evidence does not match durable GATE result")

                source = self._load_stored_experiment(
                    connection, proposal.evidence.source_experiment_id
                )
                if source.spec.fingerprint != proposal.evidence.source_experiment_fingerprint:
                    raise ResearchStoreConflictError("repair source experiment fingerprint mismatch")
                if (
                    source.result is None
                    or source.result.status is not ExperimentStatusV1.SUCCEEDED
                    or source.result.candidate_commit != proposal.evidence.source_candidate_commit
                ):
                    raise ResearchStoreConflictError("repair requires exact successful immutable source candidate")

                existing_id = connection.execute(
                    "SELECT payload_json,payload_hash FROM repair_proposals WHERE repair_id=?",
                    (proposal.repair_id,),
                ).fetchone()
                if existing_id is not None:
                    try:
                        existing = RepairProposalV1.from_dict(
                            self._decode(existing_id, label="repair proposal")
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ResearchStoreIntegrityError(f"invalid repair proposal: {exc}") from exc
                    if existing != proposal:
                        raise ResearchStoreConflictError("repair_id is immutable")
                    connection.rollback()
                    return RepairRegistrationV1(
                        proposal.repair_id,
                        proposal.repair_id,
                        proposal.fingerprint,
                        proposal.experiment.experiment_id,
                        False,
                        False,
                    )

                existing_source = connection.execute(
                    "SELECT repair_id,payload_json,payload_hash FROM repair_proposals "
                    "WHERE campaign_id=? AND source_evaluation_id=?",
                    (proposal.campaign_id, proposal.evidence.gate_evaluation_id),
                ).fetchone()
                if existing_source is not None:
                    try:
                        existing = RepairProposalV1.from_dict(
                            self._decode(existing_source, label="repair proposal")
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ResearchStoreIntegrityError(f"invalid repair proposal: {exc}") from exc
                    if existing.fingerprint != proposal.fingerprint:
                        raise ResearchStoreConflictError(
                            "durable GATE failure already has a different repair proposal"
                        )
                    connection.rollback()
                    return RepairRegistrationV1(
                        proposal.repair_id,
                        existing.repair_id,
                        existing.fingerprint,
                        existing.experiment.experiment_id,
                        False,
                        True,
                    )

                # Hypothesis is part of the same transaction so there is no
                # durable half-state between failure evidence and repair work.
                for parent in proposal.hypothesis.parent_hypothesis_ids:
                    row = connection.execute(
                        "SELECT campaign_id FROM hypotheses WHERE hypothesis_id=?", (parent,)
                    ).fetchone()
                    if row is None or row["campaign_id"] != proposal.campaign_id:
                        raise ResearchStoreConflictError(
                            f"repair hypothesis parent is missing or cross-campaign: {parent}"
                        )
                hypothesis_row = connection.execute(
                    "SELECT payload_json,payload_hash FROM hypotheses WHERE hypothesis_id=?",
                    (proposal.hypothesis.hypothesis_id,),
                ).fetchone()
                if hypothesis_row is None:
                    h_json, h_hash = self._encode(proposal.hypothesis.to_dict())
                    connection.execute(
                        "INSERT INTO hypotheses(hypothesis_id,campaign_id,status,created_at,payload_json,payload_hash) "
                        "VALUES(?,?,?,?,?,?)",
                        (
                            proposal.hypothesis.hypothesis_id,
                            proposal.campaign_id,
                            proposal.hypothesis.status.value,
                            proposal.hypothesis.created_at,
                            h_json,
                            h_hash,
                        ),
                    )
                else:
                    try:
                        loaded_h = HypothesisV1.from_dict(
                            self._decode(hypothesis_row, label="repair hypothesis")
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ResearchStoreIntegrityError(f"invalid repair hypothesis: {exc}") from exc
                    if loaded_h != proposal.hypothesis:
                        raise ResearchStoreConflictError("repair hypothesis_id is immutable")

                spec = proposal.experiment
                evaluator = connection.execute(
                    "SELECT contract_hash,dataset_fingerprint FROM evaluation_contracts WHERE contract_hash=?",
                    (spec.evaluation_contract_hash,),
                ).fetchone()
                if evaluator is None:
                    raise ResearchStoreConflictError("repair evaluation contract is not registered")
                if evaluator["dataset_fingerprint"] != spec.dataset_fingerprint:
                    raise ResearchStoreConflictError(
                        "repair experiment dataset fingerprint disagrees with evaluation contract"
                    )
                for parent in spec.parent_experiment_ids:
                    row = connection.execute(
                        "SELECT campaign_id FROM experiments WHERE experiment_id=?", (parent,)
                    ).fetchone()
                    if row is None or row["campaign_id"] != proposal.campaign_id:
                        raise ResearchStoreConflictError(
                            f"repair experiment parent is missing or cross-campaign: {parent}"
                        )
                experiment_row = connection.execute(
                    "SELECT payload_json,payload_hash FROM experiments WHERE experiment_id=?",
                    (spec.experiment_id,),
                ).fetchone()
                if experiment_row is None:
                    duplicate = connection.execute(
                        "SELECT experiment_id FROM experiments WHERE campaign_id=? AND fingerprint=?",
                        (spec.campaign_id, spec.fingerprint),
                    ).fetchone()
                    if duplicate is not None:
                        raise ResearchStoreConflictError(
                            "repair experiment fingerprint already belongs to another experiment_id"
                        )
                    e_json, e_hash = self._encode(spec.to_dict())
                    connection.execute(
                        """INSERT INTO experiments(
                            experiment_id,campaign_id,hypothesis_id,fingerprint,strategy_id,model_id,provider,model_version,
                            evaluation_contract_hash,dataset_fingerprint,seed,created_at,payload_json,payload_hash
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            spec.experiment_id,
                            spec.campaign_id,
                            spec.hypothesis_id,
                            spec.fingerprint,
                            spec.strategy_id,
                            spec.model_id,
                            spec.provider,
                            spec.model_version,
                            spec.evaluation_contract_hash,
                            spec.dataset_fingerprint,
                            spec.seed,
                            spec.created_at,
                            e_json,
                            e_hash,
                        ),
                    )
                    for ordinal, parent in enumerate(spec.parent_experiment_ids):
                        connection.execute(
                            "INSERT INTO experiment_relations(experiment_id,parent_experiment_id,ordinal) VALUES(?,?,?)",
                            (spec.experiment_id, parent, ordinal),
                        )
                else:
                    try:
                        loaded_e = ExperimentSpecV1.from_dict(
                            self._decode(experiment_row, label="repair experiment spec")
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ResearchStoreIntegrityError(f"invalid repair experiment spec: {exc}") from exc
                    if loaded_e != spec:
                        raise ResearchStoreConflictError("repair experiment_id is immutable")

                payload_json, payload_hash = self._encode(proposal.to_dict())
                connection.execute(
                    """INSERT INTO repair_proposals(
                        repair_id,campaign_id,source_evaluation_id,source_experiment_id,source_candidate_commit,
                        evidence_fingerprint,repair_hypothesis_id,repair_experiment_id,repair_experiment_fingerprint,
                        repair_depth,fingerprint,created_at,payload_json,payload_hash
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        proposal.repair_id,
                        proposal.campaign_id,
                        proposal.evidence.gate_evaluation_id,
                        proposal.evidence.source_experiment_id,
                        proposal.evidence.source_candidate_commit,
                        proposal.evidence.fingerprint,
                        proposal.hypothesis.hypothesis_id,
                        proposal.experiment.experiment_id,
                        proposal.experiment.fingerprint,
                        proposal.repair_depth,
                        proposal.fingerprint,
                        proposal.created_at,
                        payload_json,
                        payload_hash,
                    ),
                )
                connection.commit()
                return RepairRegistrationV1(
                    proposal.repair_id,
                    proposal.repair_id,
                    proposal.fingerprint,
                    proposal.experiment.experiment_id,
                    True,
                    False,
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResearchStoreConflictError(f"repair proposal registration conflict: {exc}") from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot register repair proposal: {exc}") from exc

    def load_repair_proposal(self, repair_id: str) -> RepairProposalV1:
        safe_id(repair_id, "repair_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,payload_hash FROM repair_proposals WHERE repair_id=?", (repair_id,)
            ).fetchone()
            if row is None:
                raise ResearchStoreNotFoundError(f"repair proposal not found: {repair_id}")
            try:
                return RepairProposalV1.from_dict(self._decode(row, label="repair proposal"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid repair proposal {repair_id}: {exc}") from exc

    def find_repair_by_source_evaluation(
        self, campaign_id: str, evaluation_id: str
    ) -> RepairProposalV1 | None:
        safe_id(campaign_id, "campaign_id")
        safe_id(evaluation_id, "evaluation_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,payload_hash FROM repair_proposals "
                "WHERE campaign_id=? AND source_evaluation_id=?",
                (campaign_id, evaluation_id),
            ).fetchone()
            if row is None:
                return None
            try:
                return RepairProposalV1.from_dict(self._decode(row, label="repair proposal"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid repair proposal: {exc}") from exc

    def list_repair_proposals(self, campaign_id: str, *, limit: int = 1000) -> tuple[RepairProposalV1, ...]:
        safe_id(campaign_id, "campaign_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be integer in [1,100000]")
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            rows = connection.execute(
                "SELECT payload_json,payload_hash FROM repair_proposals WHERE campaign_id=? "
                "ORDER BY created_at,repair_id LIMIT ?",
                (campaign_id, limit),
            ).fetchall()
            result = []
            for row in rows:
                try:
                    result.append(RepairProposalV1.from_dict(self._decode(row, label="repair proposal")))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ResearchStoreIntegrityError(f"invalid repair proposal: {exc}") from exc
            return tuple(result)

    def repair_depth_for_experiment(self, campaign_id: str, experiment_id: str) -> int:
        safe_id(campaign_id, "campaign_id")
        safe_id(experiment_id, "experiment_id")
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            row = connection.execute(
                "SELECT repair_depth FROM repair_proposals WHERE campaign_id=? AND repair_experiment_id=?",
                (campaign_id, experiment_id),
            ).fetchone()
            return 0 if row is None else int(row["repair_depth"])

    def _save_result_in_connection(self, connection: sqlite3.Connection, result: ExperimentResultV1) -> bool:
        payload_json, payload_hash = self._encode(result.to_dict())
        spec_row = connection.execute(
            "SELECT campaign_id FROM experiments WHERE experiment_id=?", (result.experiment_id,)
        ).fetchone()
        if spec_row is None:
            raise ResearchStoreNotFoundError(f"experiment not found: {result.experiment_id}")
        if spec_row["campaign_id"] != result.campaign_id:
            raise ResearchStoreConflictError("experiment result campaign_id mismatch")
        current_row = connection.execute(
            "SELECT * FROM experiment_results WHERE experiment_id=?", (result.experiment_id,)
        ).fetchone()
        if current_row is not None:
            try:
                current = ExperimentResultV1.from_dict(self._decode(current_row, label="experiment result"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid experiment result: {exc}") from exc
            if current == result:
                return False
            if current.is_terminal:
                raise ResearchStoreConflictError("terminal experiment result is immutable")
            if not self._valid_result_transition(current.status, result.status):
                raise ResearchStoreConflictError(
                    f"invalid experiment result transition {current.status.value}->{result.status.value}"
                )
            connection.execute(
                """UPDATE experiment_results SET
                   status=?,primary_metric=?,started_at=?,ended_at=?,decision=?,failure_kind=?,payload_json=?,payload_hash=?
                   WHERE experiment_id=?""",
                (
                    result.status.value,
                    result.primary_metric,
                    result.started_at,
                    result.ended_at,
                    None if result.decision is None else result.decision.value,
                    None if result.failure_kind is None else result.failure_kind.value,
                    payload_json,
                    payload_hash,
                    result.experiment_id,
                ),
            )
        else:
            connection.execute(
                """INSERT INTO experiment_results(
                   experiment_id,campaign_id,status,primary_metric,started_at,ended_at,decision,failure_kind,payload_json,payload_hash
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    result.experiment_id,
                    result.campaign_id,
                    result.status.value,
                    result.primary_metric,
                    result.started_at,
                    result.ended_at,
                    None if result.decision is None else result.decision.value,
                    None if result.failure_kind is None else result.failure_kind.value,
                    payload_json,
                    payload_hash,
                ),
            )
        self._replace_result_projection(connection, result)
        return True

    def save_result(self, result: ExperimentResultV1) -> None:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                changed = self._save_result_in_connection(connection, result)
                if changed:
                    connection.commit()
                else:
                    connection.rollback()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot save experiment result: {exc}") from exc

    @staticmethod
    def _valid_result_transition(old: ExperimentStatusV1, new: ExperimentStatusV1) -> bool:
        if old is ExperimentStatusV1.PLANNED:
            return new is ExperimentStatusV1.RUNNING or new in _TERMINAL_STATUSES
        if old is ExperimentStatusV1.RUNNING:
            return new in _TERMINAL_STATUSES
        return False

    @staticmethod
    def _replace_result_projection(connection: sqlite3.Connection, result: ExperimentResultV1) -> None:
        connection.execute("DELETE FROM experiment_metrics WHERE experiment_id=?", (result.experiment_id,))
        if result.primary_metric is not None:
            connection.execute(
                "INSERT INTO experiment_metrics(experiment_id,metric_name,metric_value,is_primary) VALUES(?,?,?,1)",
                (result.experiment_id, "__primary__", result.primary_metric),
            )
        for name, value in result.secondary_metrics.items():
            connection.execute(
                "INSERT INTO experiment_metrics(experiment_id,metric_name,metric_value,is_primary) VALUES(?,?,?,0)",
                (result.experiment_id, name, value),
            )
        connection.execute("DELETE FROM experiment_artifacts WHERE experiment_id=?", (result.experiment_id,))
        for ordinal, ref in enumerate(result.artifact_refs):
            connection.execute(
                "INSERT INTO experiment_artifacts(experiment_id,artifact_ref,artifact_kind,ordinal) VALUES(?,?,?,?)",
                (result.experiment_id, ref, "ARTIFACT", ordinal),
            )
        if result.stdout_ref is not None:
            connection.execute(
                "INSERT INTO experiment_artifacts(experiment_id,artifact_ref,artifact_kind,ordinal) VALUES(?,?,?,0)",
                (result.experiment_id, result.stdout_ref, "STDOUT"),
            )
        if result.stderr_ref is not None:
            connection.execute(
                "INSERT INTO experiment_artifacts(experiment_id,artifact_ref,artifact_kind,ordinal) VALUES(?,?,?,0)",
                (result.experiment_id, result.stderr_ref, "STDERR"),
            )

    def load_result(self, experiment_id: str) -> ExperimentResultV1 | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM experiment_results WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
            if row is None:
                return None
            try:
                return ExperimentResultV1.from_dict(self._decode(row, label="experiment result"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid experiment result {experiment_id}: {exc}") from exc

    def _load_stored_experiment(self, connection: sqlite3.Connection, experiment_id: str) -> StoredExperimentV1:
        spec_row = connection.execute(
            "SELECT payload_json,payload_hash FROM experiments WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
        if spec_row is None:
            raise ResearchStoreNotFoundError(f"experiment not found: {experiment_id}")
        result_row = connection.execute(
            "SELECT * FROM experiment_results WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
        try:
            spec = ExperimentSpecV1.from_dict(self._decode(spec_row, label="experiment spec"))
            result = None if result_row is None else ExperimentResultV1.from_dict(
                self._decode(result_row, label="experiment result")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchStoreIntegrityError(f"invalid stored experiment {experiment_id}: {exc}") from exc
        return StoredExperimentV1(spec, result)

    @staticmethod
    def _decode_stored_experiment_projection(row: sqlite3.Row) -> StoredExperimentV1:
        """Decode one joined experiment/result row without N+1 store reads."""
        experiment_id = str(row["experiment_id"])
        try:
            spec_payload = json.loads(row["spec_payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ResearchStoreIntegrityError(
                f"corrupt experiment spec JSON for {experiment_id}"
            ) from exc
        if not isinstance(spec_payload, Mapping):
            raise ResearchStoreIntegrityError(
                f"experiment spec payload must be object for {experiment_id}"
            )
        if row["spec_payload_hash"] != stable_sha256(spec_payload):
            raise ResearchStoreIntegrityError(
                f"experiment spec payload hash mismatch for {experiment_id}"
            )

        result_payload_json = row["result_payload_json"]
        result_payload = None
        if result_payload_json is not None:
            try:
                result_payload = json.loads(result_payload_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ResearchStoreIntegrityError(
                    f"corrupt experiment result JSON for {experiment_id}"
                ) from exc
            if not isinstance(result_payload, Mapping):
                raise ResearchStoreIntegrityError(
                    f"experiment result payload must be object for {experiment_id}"
                )
            if row["result_payload_hash"] != stable_sha256(result_payload):
                raise ResearchStoreIntegrityError(
                    f"experiment result payload hash mismatch for {experiment_id}"
                )
        elif row["result_payload_hash"] is not None:
            raise ResearchStoreIntegrityError(
                f"experiment result payload projection mismatch for {experiment_id}"
            )

        try:
            spec = ExperimentSpecV1.from_dict(spec_payload)
            result = (
                None
                if result_payload is None
                else ExperimentResultV1.from_dict(result_payload)
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchStoreIntegrityError(
                f"invalid stored experiment {experiment_id}: {exc}"
            ) from exc

        if spec.experiment_id != experiment_id:
            raise ResearchStoreIntegrityError(
                f"experiment spec identity projection mismatch for {experiment_id}"
            )
        if result is not None:
            if result.experiment_id != experiment_id:
                raise ResearchStoreIntegrityError(
                    f"experiment result identity projection mismatch for {experiment_id}"
                )
            if result.campaign_id != spec.campaign_id:
                raise ResearchStoreIntegrityError(
                    f"experiment result campaign projection mismatch for {experiment_id}"
                )
        return StoredExperimentV1(spec, result)

    def list_experiments(
        self,
        campaign_id: str,
        *,
        statuses: Iterable[ExperimentStatusV1] | None = None,
        strategy_id: str | None = None,
        model_id: str | None = None,
        evaluation_contract_hash: str | None = None,
        terminal_only: bool = False,
        limit: int = 1000,
    ) -> tuple[StoredExperimentV1, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be integer in [1,100000]")
        clauses = ["e.campaign_id=?"]
        args: list[Any] = [campaign_id]
        if strategy_id is not None:
            clauses.append("e.strategy_id=?")
            args.append(strategy_id)
        if model_id is not None:
            clauses.append("e.model_id=?")
            args.append(model_id)
        if evaluation_contract_hash is not None:
            clauses.append("e.evaluation_contract_hash=?")
            args.append(evaluation_contract_hash)
        statuses_tuple = tuple(statuses or ())
        if statuses_tuple:
            if any(not isinstance(item, ExperimentStatusV1) for item in statuses_tuple):
                raise ValueError("statuses must contain ExperimentStatusV1")
            placeholders = ",".join("?" for _ in statuses_tuple)
            clauses.append(f"COALESCE(r.status, '{ExperimentStatusV1.PLANNED.value}') IN ({placeholders})")
            args.extend(item.value for item in statuses_tuple)
        if terminal_only:
            placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
            clauses.append(f"r.status IN ({placeholders})")
            args.extend(item.value for item in sorted(_TERMINAL_STATUSES, key=lambda item: item.value))
        args.append(limit)
        sql = f"""SELECT
                      e.experiment_id,
                      e.payload_json AS spec_payload_json,
                      e.payload_hash AS spec_payload_hash,
                      r.payload_json AS result_payload_json,
                      r.payload_hash AS result_payload_hash
                  FROM experiments e
                  LEFT JOIN experiment_results r ON r.experiment_id=e.experiment_id
                  WHERE {' AND '.join(clauses)}
                  ORDER BY e.created_at,e.experiment_id LIMIT ?"""
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            rows = connection.execute(sql, tuple(args)).fetchall()
            return tuple(self._decode_stored_experiment_projection(row) for row in rows)

    def best_experiment(
        self,
        campaign_id: str,
        *,
        evaluation_contract_hash: str,
        direction: MetricDirectionV1,
    ) -> StoredExperimentV1 | None:
        if not isinstance(direction, MetricDirectionV1):
            raise ValueError("direction must be MetricDirectionV1")
        order = "DESC" if direction is MetricDirectionV1.MAXIMIZE else "ASC"
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            row = connection.execute(
                f"""SELECT e.experiment_id FROM experiments e
                    JOIN experiment_results r ON r.experiment_id=e.experiment_id
                    WHERE e.campaign_id=? AND e.evaluation_contract_hash=?
                      AND r.status=? AND r.primary_metric IS NOT NULL
                    ORDER BY r.primary_metric {order}, r.ended_at ASC, e.experiment_id ASC LIMIT 1""",
                (campaign_id, evaluation_contract_hash, ExperimentStatusV1.SUCCEEDED.value),
            ).fetchone()
            return None if row is None else self._load_stored_experiment(connection, row["experiment_id"])

    def _save_decision_in_connection(self, connection: sqlite3.Connection, decision: SearchDecisionV1) -> bool:
        payload_json, payload_hash = self._encode(decision.to_dict())
        self._require_campaign(connection, decision.campaign_id)
        ids = (decision.experiment_id,) + decision.evidence_experiment_ids
        if decision.incumbent_experiment_id is not None:
            ids += (decision.incumbent_experiment_id,)
        for experiment_id in dict.fromkeys(ids):
            row = connection.execute(
                "SELECT campaign_id FROM experiments WHERE experiment_id=?", (experiment_id,)
            ).fetchone()
            if row is None or row["campaign_id"] != decision.campaign_id:
                raise ResearchStoreConflictError(
                    f"decision references missing or cross-campaign experiment: {experiment_id}"
                )
        target_result = connection.execute(
            "SELECT status FROM experiment_results WHERE experiment_id=?", (decision.experiment_id,)
        ).fetchone()
        if target_result is None or ExperimentStatusV1(target_result["status"]) not in _TERMINAL_STATUSES:
            raise ResearchStoreConflictError("search decision requires terminal experiment result")
        by_experiment = connection.execute(
            "SELECT decision_id,payload_json,payload_hash FROM search_decisions WHERE campaign_id=? AND experiment_id=?",
            (decision.campaign_id, decision.experiment_id),
        ).fetchone()
        if by_experiment is not None and by_experiment["decision_id"] != decision.decision_id:
            raise ResearchStoreConflictError(
                f"experiment already has durable search decision: {decision.experiment_id}"
            )
        existing = connection.execute(
            "SELECT payload_json,payload_hash FROM search_decisions WHERE decision_id=?", (decision.decision_id,)
        ).fetchone()
        if existing is not None:
            try:
                loaded = SearchDecisionV1.from_dict(self._decode(existing, label="search decision"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid search decision: {exc}") from exc
            if loaded != decision:
                raise ResearchStoreConflictError("decision_id is immutable")
            return False
        connection.execute(
            """INSERT INTO search_decisions(
                decision_id,campaign_id,experiment_id,action,incumbent_experiment_id,created_at,payload_json,payload_hash
                ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                decision.decision_id,
                decision.campaign_id,
                decision.experiment_id,
                decision.action.value,
                decision.incumbent_experiment_id,
                decision.created_at,
                payload_json,
                payload_hash,
            ),
        )
        return True

    def save_decision(self, decision: SearchDecisionV1) -> None:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                changed = self._save_decision_in_connection(connection, decision)
                if changed:
                    connection.commit()
                else:
                    connection.rollback()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot save search decision: {exc}") from exc

    def save_terminal_outcome(self, result: ExperimentResultV1, decision: SearchDecisionV1) -> None:
        if not result.is_terminal:
            raise ResearchStoreConflictError("atomic terminal outcome requires terminal ExperimentResultV1")
        if result.campaign_id != decision.campaign_id or result.experiment_id != decision.experiment_id:
            raise ResearchStoreConflictError("terminal result/search decision identity mismatch")
        if result.decision is not decision.action:
            raise ResearchStoreConflictError("terminal result decision must equal SearchDecision action")
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                result_changed = self._save_result_in_connection(connection, result)
                decision_changed = self._save_decision_in_connection(connection, decision)
                if result_changed or decision_changed:
                    connection.commit()
                else:
                    connection.rollback()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot save atomic terminal research outcome: {exc}") from exc

    def list_decisions(self, campaign_id: str, *, limit: int = 1000) -> tuple[SearchDecisionV1, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be integer in [1,100000]")
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            rows = connection.execute(
                """SELECT payload_json,payload_hash FROM search_decisions
                   WHERE campaign_id=? ORDER BY created_at,decision_id LIMIT ?""",
                (campaign_id, limit),
            ).fetchall()
            decisions: list[SearchDecisionV1] = []
            for row in rows:
                try:
                    decisions.append(SearchDecisionV1.from_dict(self._decode(row, label="search decision")))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ResearchStoreIntegrityError(f"invalid search decision: {exc}") from exc
            return tuple(decisions)

    def champion_experiment_id(self, campaign_id: str) -> str | None:
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            row = connection.execute(
                """SELECT experiment_id FROM search_decisions
                   WHERE campaign_id=? AND action=?
                   ORDER BY created_at DESC, decision_id DESC LIMIT 1""",
                (campaign_id, SearchDecisionActionV1.UPDATE_CHAMPION.value),
            ).fetchone()
            return None if row is None else str(row["experiment_id"])

    def _validate_typed_insight_evidence(
        self,
        connection: sqlite3.Connection,
        insight: ResearchInsightRecordV1,
    ) -> None:
        evaluator = connection.execute(
            "SELECT contract_hash FROM evaluation_contracts WHERE contract_hash=?",
            (insight.evaluation_contract_hash,),
        ).fetchone()
        if evaluator is None:
            raise ResearchStoreConflictError("typed insight evaluation contract is not registered")
        if insight.hypothesis_id is not None:
            row = connection.execute(
                "SELECT campaign_id FROM hypotheses WHERE hypothesis_id=?",
                (insight.hypothesis_id,),
            ).fetchone()
            if row is None or row["campaign_id"] != insight.campaign_id:
                raise ResearchStoreConflictError("typed insight hypothesis is missing or cross-campaign")

        for evidence in insight.evidence:
            source_experiment = self._load_stored_experiment(connection, evidence.experiment_id)
            if source_experiment.spec.campaign_id != insight.campaign_id:
                raise ResearchStoreConflictError("typed insight evidence experiment is cross-campaign")
            expected: str
            if evidence.source_kind is ResearchInsightEvidenceKindV1.EXPERIMENT_RESULT:
                if evidence.source_id != evidence.experiment_id:
                    raise ResearchStoreConflictError("DEV insight evidence source_id must equal experiment_id")
                if source_experiment.result is None or not source_experiment.result.is_terminal:
                    raise ResearchStoreConflictError("DEV insight evidence requires terminal experiment result")
                expected = experiment_result_evidence_fingerprint(
                    source_experiment.spec,
                    source_experiment.result,
                )
            elif evidence.source_kind is ResearchInsightEvidenceKindV1.GATE_EVALUATION:
                gate = self._load_stored_candidate_evaluation(connection, evidence.source_id)
                if gate.spec.campaign_id != insight.campaign_id:
                    raise ResearchStoreConflictError("GATE insight evidence is cross-campaign")
                if gate.spec.surface is not CandidateEvaluationSurfaceV1.GATE:
                    raise ResearchStoreConflictError("TEST evidence cannot be persisted as typed research insight")
                if gate.spec.candidate.source_experiment_id != evidence.experiment_id:
                    raise ResearchStoreConflictError("GATE insight evidence experiment identity mismatch")
                expected = gate_evaluation_evidence_fingerprint(gate)
            elif evidence.source_kind is ResearchInsightEvidenceKindV1.REPAIR_OUTCOME:
                row = connection.execute(
                    "SELECT payload_json,payload_hash FROM repair_proposals WHERE repair_id=?",
                    (evidence.source_id,),
                ).fetchone()
                if row is None:
                    raise ResearchStoreConflictError("repair insight evidence proposal is missing")
                try:
                    proposal = RepairProposalV1.from_dict(self._decode(row, label="repair proposal"))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ResearchStoreIntegrityError(f"invalid repair proposal: {exc}") from exc
                if proposal.campaign_id != insight.campaign_id:
                    raise ResearchStoreConflictError("repair insight evidence is cross-campaign")
                if proposal.experiment.experiment_id != evidence.experiment_id:
                    raise ResearchStoreConflictError("repair insight evidence experiment identity mismatch")
                expected = repair_outcome_evidence_fingerprint(proposal, source_experiment)
            else:  # pragma: no cover - exhaustive enum guard
                raise ResearchStoreConflictError("unsupported typed insight evidence kind")
            if expected != evidence.source_fingerprint:
                raise ResearchStoreConflictError("typed insight evidence fingerprint does not match durable source")

    def register_typed_insight(self, insight: ResearchInsightRecordV1) -> ResearchInsightRegistrationV1:
        return self.register_typed_insights((insight,))[0]

    def register_typed_insights(
        self,
        insights: Sequence[ResearchInsightRecordV1],
    ) -> tuple[ResearchInsightRegistrationV1, ...]:
        if not insights:
            return ()
        if any(not isinstance(item, ResearchInsightRecordV1) for item in insights):
            raise TypeError("insights must contain ResearchInsightRecordV1")
        registrations: list[ResearchInsightRegistrationV1] = []
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for insight in insights:
                    self._require_campaign(connection, insight.campaign_id)
                    existing = connection.execute(
                        "SELECT payload_json,payload_hash FROM typed_research_insights WHERE insight_id=?",
                        (insight.insight_id,),
                    ).fetchone()
                    if existing is not None:
                        try:
                            loaded = ResearchInsightRecordV1.from_dict(
                                self._decode(existing, label="typed research insight")
                            )
                        except (KeyError, TypeError, ValueError) as exc:
                            raise ResearchStoreIntegrityError(f"invalid typed research insight: {exc}") from exc
                        if loaded.fingerprint == insight.fingerprint:
                            registrations.append(
                                ResearchInsightRegistrationV1(
                                    insight.insight_id,
                                    insight.insight_id,
                                    insight.fingerprint,
                                    False,
                                    loaded != insight,
                                )
                            )
                            continue

                        self._validate_typed_insight_evidence(connection, insight)
                        raise ResearchStoreConflictError("typed insight_id is immutable")

                    self._validate_typed_insight_evidence(connection, insight)
                    duplicate = connection.execute(
                        "SELECT insight_id FROM typed_research_insights WHERE campaign_id=? AND fingerprint=?",
                        (insight.campaign_id, insight.fingerprint),
                    ).fetchone()
                    if duplicate is not None:
                        registrations.append(
                            ResearchInsightRegistrationV1(
                                insight.insight_id,
                                str(duplicate["insight_id"]),
                                insight.fingerprint,
                                False,
                                True,
                            )
                        )
                        continue
                    payload_json, payload_hash = self._encode(insight.to_dict())
                    connection.execute(
                        """INSERT INTO typed_research_insights(
                            insight_id,campaign_id,fingerprint,kind,polarity,disposition,surface,subject_key,
                            evaluation_contract_hash,hypothesis_id,strategy_id,confidence,created_at,payload_json,payload_hash
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            insight.insight_id,
                            insight.campaign_id,
                            insight.fingerprint,
                            insight.kind.value,
                            insight.polarity.value,
                            insight.disposition.value,
                            insight.surface.value,
                            insight.subject_key,
                            insight.evaluation_contract_hash,
                            insight.hypothesis_id,
                            insight.strategy_id,
                            insight.confidence,
                            insight.created_at,
                            payload_json,
                            payload_hash,
                        ),
                    )
                    for ordinal, evidence in enumerate(insight.evidence):
                        connection.execute(
                            """INSERT INTO typed_research_insight_evidence(
                                insight_id,ordinal,source_kind,source_id,source_fingerprint,experiment_id
                            ) VALUES(?,?,?,?,?,?)""",
                            (
                                insight.insight_id,
                                ordinal,
                                evidence.source_kind.value,
                                evidence.source_id,
                                evidence.source_fingerprint,
                                evidence.experiment_id,
                            ),
                        )
                    registrations.append(
                        ResearchInsightRegistrationV1(
                            insight.insight_id,
                            insight.insight_id,
                            insight.fingerprint,
                            True,
                            False,
                        )
                    )
                connection.commit()
                return tuple(registrations)
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResearchStoreConflictError(f"typed insight registration conflict: {exc}") from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot register typed research insight: {exc}") from exc

    def load_typed_insight(self, insight_id: str) -> ResearchInsightRecordV1:
        safe_id(insight_id, "insight_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,payload_hash FROM typed_research_insights WHERE insight_id=?",
                (insight_id,),
            ).fetchone()
            if row is None:
                raise ResearchStoreNotFoundError(f"typed research insight not found: {insight_id}")
            try:
                return ResearchInsightRecordV1.from_dict(self._decode(row, label="typed research insight"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid typed research insight {insight_id}: {exc}") from exc

    def find_typed_insight_by_fingerprint(
        self,
        campaign_id: str,
        fingerprint: str,
    ) -> ResearchInsightRecordV1 | None:
        safe_id(campaign_id, "campaign_id")
        fingerprint = sha256_hex(fingerprint, "fingerprint")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json,payload_hash FROM typed_research_insights WHERE campaign_id=? AND fingerprint=?",
                (campaign_id, fingerprint),
            ).fetchone()
            if row is None:
                return None
            try:
                return ResearchInsightRecordV1.from_dict(self._decode(row, label="typed research insight"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid typed research insight: {exc}") from exc

    def list_typed_insights(
        self,
        campaign_id: str,
        *,
        disposition: ResearchInsightDispositionV1 | None = None,
        surface: ResearchInsightSurfaceV1 | None = None,
        evaluation_contract_hash: str | None = None,
        limit: int = 1000,
    ) -> tuple[ResearchInsightRecordV1, ...]:
        safe_id(campaign_id, "campaign_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise ValueError("limit must be integer in [1,100000]")
        clauses = ["campaign_id=?"]
        args: list[Any] = [campaign_id]
        if disposition is not None:
            if not isinstance(disposition, ResearchInsightDispositionV1):
                raise ValueError("disposition must be ResearchInsightDispositionV1")
            clauses.append("disposition=?")
            args.append(disposition.value)
        if surface is not None:
            if not isinstance(surface, ResearchInsightSurfaceV1):
                raise ValueError("surface must be ResearchInsightSurfaceV1")
            clauses.append("surface=?")
            args.append(surface.value)
        if evaluation_contract_hash is not None:
            clauses.append("evaluation_contract_hash=?")
            args.append(sha256_hex(evaluation_contract_hash, "evaluation_contract_hash"))
        args.append(limit)
        with self._connect() as connection:
            self._require_campaign(connection, campaign_id)
            rows = connection.execute(
                "SELECT payload_json,payload_hash FROM typed_research_insights WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at,insight_id LIMIT ?",
                tuple(args),
            ).fetchall()
            result: list[ResearchInsightRecordV1] = []
            for row in rows:
                try:
                    result.append(ResearchInsightRecordV1.from_dict(self._decode(row, label="typed research insight")))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ResearchStoreIntegrityError(f"invalid typed research insight: {exc}") from exc
            return tuple(result)

    def save_insight(self, insight: ResearchInsightV1) -> None:
        payload_json, payload_hash = self._encode(insight.to_dict())
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_campaign(connection, insight.campaign_id)
                for experiment_id in insight.support_experiment_ids + insight.contradicting_experiment_ids:
                    row = connection.execute(
                        "SELECT campaign_id FROM experiments WHERE experiment_id=?", (experiment_id,)
                    ).fetchone()
                    if row is None or row["campaign_id"] != insight.campaign_id:
                        raise ResearchStoreConflictError(
                            f"insight references missing or cross-campaign experiment: {experiment_id}"
                        )
                existing = connection.execute(
                    "SELECT payload_json,payload_hash FROM research_insights WHERE insight_id=?", (insight.insight_id,)
                ).fetchone()
                if existing is not None:
                    try:
                        loaded = ResearchInsightV1.from_dict(self._decode(existing, label="research insight"))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ResearchStoreIntegrityError(f"invalid research insight: {exc}") from exc
                    if loaded != insight:
                        raise ResearchStoreConflictError("insight_id is immutable")
                    connection.rollback()
                    return
                connection.execute(
                    """INSERT INTO research_insights(
                        insight_id,campaign_id,created_at,confidence,scope,payload_json,payload_hash
                        ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        insight.insight_id,
                        insight.campaign_id,
                        insight.created_at,
                        insight.confidence,
                        insight.scope,
                        payload_json,
                        payload_hash,
                    ),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot save research insight: {exc}") from exc

    def link_human_request(
        self,
        *,
        campaign_id: str,
        request_id: str,
        created_at: str,
        experiment_id: str | None = None,
    ) -> None:
        safe_id(request_id, "request_id")
        created_at = timestamp(created_at, "created_at")
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._require_campaign(connection, campaign_id)
                if experiment_id is not None:
                    row = connection.execute(
                        "SELECT campaign_id FROM experiments WHERE experiment_id=?", (experiment_id,)
                    ).fetchone()
                    if row is None or row["campaign_id"] != campaign_id:
                        raise ResearchStoreConflictError("human request experiment is missing or cross-campaign")
                existing = connection.execute(
                    "SELECT experiment_id,created_at FROM human_requests_link WHERE campaign_id=? AND request_id=?",
                    (campaign_id, request_id),
                ).fetchone()
                if existing is not None:
                    if existing["experiment_id"] == experiment_id and existing["created_at"] == created_at:
                        connection.rollback()
                        return
                    raise ResearchStoreConflictError("human request link is immutable")
                connection.execute(
                    "INSERT INTO human_requests_link(campaign_id,request_id,experiment_id,created_at) VALUES(?,?,?,?)",
                    (campaign_id, request_id, experiment_id, created_at),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise ResearchStoreError(f"cannot link human request: {exc}") from exc

    def campaign_summary(self, campaign_id: str) -> ResearchCampaignSummaryV1:
        with self._connect() as connection:
            campaign = self._require_campaign(connection, campaign_id)
            rows = connection.execute(
                """SELECT COALESCE(r.status, ?) AS status, COUNT(*) AS n
                   FROM experiments e LEFT JOIN experiment_results r ON r.experiment_id=e.experiment_id
                   WHERE e.campaign_id=? GROUP BY COALESCE(r.status, ?)""",
                (ExperimentStatusV1.PLANNED.value, campaign_id, ExperimentStatusV1.PLANNED.value),
            ).fetchall()
            counts = {str(row["status"]): int(row["n"]) for row in rows}
            experiment_count = sum(counts.values())
            terminal_count = sum(counts.get(status.value, 0) for status in _TERMINAL_STATUSES)
            current_contract = self.load_research_contract(campaign["current_contract_hash"], _connection=connection)
            assert current_contract is not None
            champion_row = connection.execute(
                """SELECT experiment_id FROM search_decisions WHERE campaign_id=? AND action=?
                   ORDER BY created_at DESC,decision_id DESC LIMIT 1""",
                (campaign_id, SearchDecisionActionV1.UPDATE_CHAMPION.value),
            ).fetchone()
            comparable_hashes = connection.execute(
                """SELECT DISTINCT e.evaluation_contract_hash FROM experiments e
                   JOIN experiment_results r ON r.experiment_id=e.experiment_id
                   WHERE e.campaign_id=? AND r.status=? AND r.primary_metric IS NOT NULL""",
                (campaign_id, ExperimentStatusV1.SUCCEEDED.value),
            ).fetchall()
            comparable_hash = (
                str(comparable_hashes[0]["evaluation_contract_hash"]) if len(comparable_hashes) == 1 else None
            )
            best = None
            if comparable_hash is not None:
                best = connection.execute(
                    f"""SELECT e.experiment_id,r.primary_metric FROM experiments e
                        JOIN experiment_results r ON r.experiment_id=e.experiment_id
                        WHERE e.campaign_id=? AND e.evaluation_contract_hash=?
                          AND r.status=? AND r.primary_metric IS NOT NULL
                        ORDER BY r.primary_metric {'DESC' if current_contract.metric_direction is MetricDirectionV1.MAXIMIZE else 'ASC'},
                                 r.ended_at ASC,e.experiment_id ASC LIMIT 1""",
                    (campaign_id, comparable_hash, ExperimentStatusV1.SUCCEEDED.value),
                ).fetchone()
            failed_count = sum(
                counts.get(status.value, 0)
                for status in (
                    ExperimentStatusV1.FAILED,
                    ExperimentStatusV1.CRASHED,
                    ExperimentStatusV1.INVALID,
                )
            )
            return ResearchCampaignSummaryV1(
                campaign_id=campaign_id,
                contract_hash=current_contract.contract_hash,
                experiment_count=experiment_count,
                terminal_count=terminal_count,
                succeeded_count=counts.get(ExperimentStatusV1.SUCCEEDED.value, 0),
                failed_count=failed_count,
                running_count=counts.get(ExperimentStatusV1.RUNNING.value, 0),
                planned_count=counts.get(ExperimentStatusV1.PLANNED.value, 0),
                champion_experiment_id=None if champion_row is None else str(champion_row["experiment_id"]),
                best_experiment_id=None if best is None else str(best["experiment_id"]),
                best_primary_metric=None if best is None else float(best["primary_metric"]),
                comparable_evaluation_contract_hash=comparable_hash,
            )

    def verify_integrity(self) -> None:
        with self._connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if [row[0] for row in integrity] != ["ok"]:
                raise ResearchStoreIntegrityError(f"SQLite integrity_check failed: {[row[0] for row in integrity]}")
            for campaign in connection.execute(
                "SELECT campaign_id,current_contract_hash,created_at,payload_hash FROM research_campaigns"
            ):
                self._require_campaign(connection, campaign["campaign_id"])
            checks: tuple[tuple[str, str], ...] = (
                ("research_campaign_state", "research campaign state"),
                ("research_contracts", "research contract"),
                ("evaluation_contracts", "evaluation contract"),
                ("hypotheses", "hypothesis"),
                ("experiments", "experiment spec"),
                ("experiment_results", "experiment result"),
                ("search_decisions", "search decision"),
                ("research_insights", "research insight"),
                ("candidate_evaluations", "candidate evaluation spec"),
                ("candidate_evaluation_results", "candidate evaluation result"),
                ("repair_proposals", "repair proposal"),
                ("typed_research_insights", "typed research insight"),
                ("research_strategy_checkpoints", "research strategy checkpoint"),
            )
            for table, label in checks:
                for row in connection.execute(f"SELECT payload_json,payload_hash FROM {table}"):
                    self._decode(row, label=label)
            self._verify_projections(connection)

    def _verify_projections(self, connection: sqlite3.Connection) -> None:
        for row in connection.execute("SELECT * FROM research_campaign_state"):
            try:
                state = ResearchCampaignStateV1.from_dict(self._decode(row, label="research campaign state"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid campaign state projection: {exc}") from exc
            actual = (row["campaign_id"], row["generation"], row["status"], row["updated_at"])
            expected = (state.campaign_id, state.generation, state.status.value, state.updated_at)
            if actual != expected:
                raise ResearchStoreIntegrityError(f"campaign state indexed projection mismatch: {state.campaign_id}")
            for label, experiment_id in (
                ("champion_experiment_id", state.champion_experiment_id),
                ("last_experiment_id", state.last_experiment_id),
            ):
                if experiment_id is None:
                    continue
                ref = connection.execute(
                    "SELECT campaign_id FROM experiments WHERE experiment_id=?", (experiment_id,)
                ).fetchone()
                if ref is None or ref["campaign_id"] != state.campaign_id:
                    raise ResearchStoreIntegrityError(
                        f"campaign state {label} references missing experiment: {experiment_id}"
                    )

        for row in connection.execute("SELECT * FROM research_strategy_checkpoints"):
            self._decode_strategy_checkpoint(row)

        for row in connection.execute("SELECT * FROM experiments"):
            try:
                spec = ExperimentSpecV1.from_dict(self._decode(row, label="experiment spec"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid experiment projection: {exc}") from exc
            expected = (
                spec.campaign_id, spec.hypothesis_id, spec.fingerprint, spec.strategy_id, spec.model_id, spec.provider,
                spec.model_version, spec.evaluation_contract_hash, spec.dataset_fingerprint, spec.seed, spec.created_at
            )
            actual = (
                row["campaign_id"], row["hypothesis_id"], row["fingerprint"], row["strategy_id"], row["model_id"],
                row["provider"], row["model_version"], row["evaluation_contract_hash"], row["dataset_fingerprint"],
                row["seed"], row["created_at"]
            )
            if actual != expected:
                raise ResearchStoreIntegrityError(f"experiment indexed projection mismatch: {spec.experiment_id}")
            relation_rows = connection.execute(
                "SELECT parent_experiment_id FROM experiment_relations WHERE experiment_id=? ORDER BY ordinal",
                (spec.experiment_id,),
            ).fetchall()
            if tuple(item["parent_experiment_id"] for item in relation_rows) != spec.parent_experiment_ids:
                raise ResearchStoreIntegrityError(f"experiment relation projection mismatch: {spec.experiment_id}")

        for row in connection.execute("SELECT * FROM candidate_evaluations"):
            try:
                spec = CandidateEvaluationSpecV1.from_dict(
                    self._decode(row, label="candidate evaluation spec")
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid candidate evaluation projection: {exc}") from exc
            expected = (
                spec.campaign_id,
                spec.candidate.source_experiment_id,
                spec.candidate.source_experiment_fingerprint,
                spec.candidate.candidate_commit,
                spec.fingerprint,
                spec.proposal_kind.value,
                spec.surface.value,
                spec.evaluation_contract_hash,
                spec.seed,
                spec.created_at,
            )
            actual = (
                row["campaign_id"],
                row["source_experiment_id"],
                row["source_experiment_fingerprint"],
                row["candidate_commit"],
                row["fingerprint"],
                row["proposal_kind"],
                row["surface"],
                row["evaluation_contract_hash"],
                row["seed"],
                row["created_at"],
            )
            if actual != expected:
                raise ResearchStoreIntegrityError(
                    f"candidate evaluation indexed projection mismatch: {spec.evaluation_id}"
                )
            source = self._load_stored_experiment(connection, spec.candidate.source_experiment_id)
            if (
                source.spec.campaign_id != spec.campaign_id
                or source.spec.fingerprint != spec.candidate.source_experiment_fingerprint
                or source.result is None
                or source.result.status is not ExperimentStatusV1.SUCCEEDED
                or source.result.candidate_commit != spec.candidate.candidate_commit
            ):
                raise ResearchStoreIntegrityError(
                    f"candidate evaluation source identity mismatch: {spec.evaluation_id}"
                )

        for row in connection.execute("SELECT * FROM candidate_evaluation_results"):
            try:
                result = CandidateEvaluationResultV1.from_dict(
                    self._decode(row, label="candidate evaluation result")
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid candidate evaluation result projection: {exc}") from exc
            if (
                row["campaign_id"] != result.campaign_id
                or row["status"] != result.status.value
                or row["primary_metric"] != result.primary_metric
                or row["started_at"] != result.started_at
                or row["ended_at"] != result.ended_at
                or row["failure_kind"] != (None if result.failure_kind is None else result.failure_kind.value)
            ):
                raise ResearchStoreIntegrityError(
                    f"candidate evaluation result indexed projection mismatch: {result.evaluation_id}"
                )

        for row in connection.execute("SELECT * FROM repair_proposals"):
            try:
                proposal = RepairProposalV1.from_dict(self._decode(row, label="repair proposal"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid repair proposal projection: {exc}") from exc
            actual = (
                row["repair_id"], row["campaign_id"], row["source_evaluation_id"],
                row["source_experiment_id"], row["source_candidate_commit"],
                row["evidence_fingerprint"], row["repair_hypothesis_id"],
                row["repair_experiment_id"], row["repair_experiment_fingerprint"],
                int(row["repair_depth"]), row["fingerprint"], row["created_at"],
            )
            expected = (
                proposal.repair_id, proposal.campaign_id, proposal.evidence.gate_evaluation_id,
                proposal.evidence.source_experiment_id, proposal.evidence.source_candidate_commit,
                proposal.evidence.fingerprint, proposal.hypothesis.hypothesis_id,
                proposal.experiment.experiment_id, proposal.experiment.fingerprint,
                proposal.repair_depth, proposal.fingerprint, proposal.created_at,
            )
            if actual != expected:
                raise ResearchStoreIntegrityError(
                    f"repair proposal indexed projection mismatch: {proposal.repair_id}"
                )
            gate = self._load_stored_candidate_evaluation(
                connection, proposal.evidence.gate_evaluation_id
            )
            try:
                durable_evidence = GateFailureEvidenceV1.from_candidate_evaluation(gate)
            except ValueError as exc:
                raise ResearchStoreIntegrityError(
                    f"repair proposal source is no longer valid GATE failure: {proposal.repair_id}: {exc}"
                ) from exc
            if durable_evidence != proposal.evidence:
                raise ResearchStoreIntegrityError(
                    f"repair proposal evidence mismatch: {proposal.repair_id}"
                )
            repair_experiment = self._load_stored_experiment(
                connection, proposal.experiment.experiment_id
            )
            if repair_experiment.spec != proposal.experiment:
                raise ResearchStoreIntegrityError(
                    f"repair proposal experiment mismatch: {proposal.repair_id}"
                )

        for row in connection.execute("SELECT * FROM typed_research_insights"):
            try:
                insight = ResearchInsightRecordV1.from_dict(
                    self._decode(row, label="typed research insight")
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid typed research insight projection: {exc}") from exc
            actual = (
                row["campaign_id"],
                row["fingerprint"],
                row["kind"],
                row["polarity"],
                row["disposition"],
                row["surface"],
                row["subject_key"],
                row["evaluation_contract_hash"],
                row["hypothesis_id"],
                row["strategy_id"],
                float(row["confidence"]),
                row["created_at"],
            )
            expected = (
                insight.campaign_id,
                insight.fingerprint,
                insight.kind.value,
                insight.polarity.value,
                insight.disposition.value,
                insight.surface.value,
                insight.subject_key,
                insight.evaluation_contract_hash,
                insight.hypothesis_id,
                insight.strategy_id,
                float(insight.confidence),
                insight.created_at,
            )
            if actual != expected:
                raise ResearchStoreIntegrityError(
                    f"typed research insight indexed projection mismatch: {insight.insight_id}"
                )
            evidence_rows = connection.execute(
                """SELECT ordinal,source_kind,source_id,source_fingerprint,experiment_id
                   FROM typed_research_insight_evidence WHERE insight_id=? ORDER BY ordinal""",
                (insight.insight_id,),
            ).fetchall()
            actual_evidence = tuple(
                (
                    int(item["ordinal"]),
                    item["source_kind"],
                    item["source_id"],
                    item["source_fingerprint"],
                    item["experiment_id"],
                )
                for item in evidence_rows
            )
            expected_evidence = tuple(
                (
                    ordinal,
                    item.source_kind.value,
                    item.source_id,
                    item.source_fingerprint,
                    item.experiment_id,
                )
                for ordinal, item in enumerate(insight.evidence)
            )
            if actual_evidence != expected_evidence:
                raise ResearchStoreIntegrityError(
                    f"typed research insight evidence projection mismatch: {insight.insight_id}"
                )
            try:
                self._validate_typed_insight_evidence(connection, insight)
            except ResearchStoreConflictError as exc:
                raise ResearchStoreIntegrityError(
                    f"typed research insight source integrity mismatch: {insight.insight_id}: {exc}"
                ) from exc

        for row in connection.execute("SELECT * FROM experiment_results"):
            try:
                result = ExperimentResultV1.from_dict(self._decode(row, label="experiment result"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ResearchStoreIntegrityError(f"invalid result projection: {exc}") from exc
            if (
                row["campaign_id"] != result.campaign_id
                or row["status"] != result.status.value
                or row["primary_metric"] != result.primary_metric
                or row["started_at"] != result.started_at
                or row["ended_at"] != result.ended_at
                or row["decision"] != (None if result.decision is None else result.decision.value)
                or row["failure_kind"] != (None if result.failure_kind is None else result.failure_kind.value)
            ):
                raise ResearchStoreIntegrityError(f"experiment result indexed projection mismatch: {result.experiment_id}")
            metrics = connection.execute(
                "SELECT metric_name,metric_value,is_primary FROM experiment_metrics WHERE experiment_id=?",
                (result.experiment_id,),
            ).fetchall()
            actual_metrics = {(item["metric_name"], float(item["metric_value"]), int(item["is_primary"])) for item in metrics}
            expected_metrics = {(name, float(value), 0) for name, value in result.secondary_metrics.items()}
            if result.primary_metric is not None:
                expected_metrics.add(("__primary__", float(result.primary_metric), 1))
            if actual_metrics != expected_metrics:
                raise ResearchStoreIntegrityError(f"experiment metric projection mismatch: {result.experiment_id}")
            artifacts = connection.execute(
                "SELECT artifact_ref,artifact_kind FROM experiment_artifacts WHERE experiment_id=?",
                (result.experiment_id,),
            ).fetchall()
            actual_artifacts = {(item["artifact_ref"], item["artifact_kind"]) for item in artifacts}
            expected_artifacts = {(ref, "ARTIFACT") for ref in result.artifact_refs}
            if result.stdout_ref is not None:
                expected_artifacts.add((result.stdout_ref, "STDOUT"))
            if result.stderr_ref is not None:
                expected_artifacts.add((result.stderr_ref, "STDERR"))
            if actual_artifacts != expected_artifacts:
                raise ResearchStoreIntegrityError(f"experiment artifact projection mismatch: {result.experiment_id}")

    def close(self) -> None:
        """Compatibility no-op: connections are intentionally short-lived per operation."""
        return None

# Verified compatibility aliases for historical Python interfaces.
ResearchExperimentStore._create_phase23_cycle_schema = ResearchExperimentStore._create_campaign_cycle_schema
ResearchExperimentStore._list_phase23_payloads = ResearchExperimentStore._list_campaign_cycle_payloads
ResearchExperimentStore._load_phase23_cycle_payload = ResearchExperimentStore._load_campaign_cycle_payload
ResearchExperimentStore._save_phase23_record = ResearchExperimentStore._save_campaign_cycle_record
