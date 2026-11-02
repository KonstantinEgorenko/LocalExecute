"""Verified root-only context projection; the full WorkContract stays visible."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

from ..agent_loop.protocol import decode_strict_json
from ..context_manager import Artifact, ArtifactKind, ArtifactOrigin
from ..context_manager.errors import ContextResolutionError
from ..context_manager.token_estimator import Utf8TokenEstimator
from .models import canonical_json, stable_sha256

VERIFIED_MANDATORY_CONTEXT_PROJECTION_V1 = 'VERIFIED_MANDATORY_CONTEXT_PROJECTION_V1'
POLICY = 'ROOT_EXACT_CONTRACT_DUPLICATES_V1'


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def require(value, detail):
    if not value:
        raise ContextResolutionError('ROOT_PROJECTION_INVALID: ' + detail)


def leaves(value, path=''):
    if isinstance(value, dict) and value:
        for key, item in sorted(value.items()):
            yield from leaves(item, path + '/' + key.replace('~', '~0').replace('/', '~1'))
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from leaves(item, path + '/' + str(index))
    else:
        yield path, value


@dataclass(frozen=True, slots=True)
class VerifiedRootProjectionV1:
    """Canonical immutable document, accepted only after authority recomputation."""
    document: str

    def __post_init__(self):
        body = decode_strict_json(self.document)
        keys = {'schema_version', 'mission_id', 'goal_id', 'root_task_fingerprint',
                'original_capsule_artifact_id', 'original_capsule_sha256',
                'current_todo_id', 'current_todo_objective', 'public_objective_summary',
                'context_profile_id',
                'required_dependency_summary', 'required_worker_obligations',
                'projection_policy_version', 'projection_fingerprint'}
        require(type(body) is dict and set(body) == keys, 'projection fields')
        require(type(body['schema_version']) is int and body['schema_version'] == 1, 'schema')
        require(all(type(body[k]) is str and body[k] for k in keys - {
            'schema_version', 'required_dependency_summary'}), 'projection text fields')
        dependency = body['required_dependency_summary']
        require(type(dependency) is dict and set(dependency) == {'artifact_refs_sha256', 'todo_ids'}
                and type(dependency['artifact_refs_sha256']) is str
                and type(dependency['todo_ids']) is list
                and all(type(item) is str for item in dependency['todo_ids']), 'dependency summary')
        require(body['projection_policy_version'] == POLICY, 'policy')
        require(canonical_json(body) == self.document, 'canonical document')
        require(body['projection_fingerprint'] == sha(canonical_json(
            {k: v for k, v in body.items() if k != 'projection_fingerprint'})), 'fingerprint')


class VerifiedRootProjectionAuthority:
    """Bound by production scheduler to durable Mission and work-item registry.

    Nothing is summarized: root intent is verbatim; constraints are removed only
    from the root representation after exact equality to the FULL model-visible
    contract. Coverage/provenance is durable and separate from generated text.
    """
    def __init__(self, spine, spec_provider, todo_provider, spec, eligible_profiles):
        self.spine, self.spec_provider, self.todo_provider = spine, spec_provider, todo_provider
        self.store = spine.artifact_store
        self.admitted_missions = {spec.goal_id: canonical_json(spec.to_dict())}
        self.eligible_profiles = tuple(eligible_profiles)

    def admit(self, spec):
        value = canonical_json(spec.to_dict())
        require(self.admitted_missions.get(spec.goal_id, value) == value, 'Mission binding changed')
        self.admitted_missions[spec.goal_id] = value

    def artifact(self, ref, source, origin):
        item = self.store.get_artifact(ref.artifact_id)
        require(item.ref == ref and item.sha256 == sha(item.content), 'artifact hash')
        require(item.source == source and item.origin is origin, 'artifact provenance')
        return item

    def persist(self, content, source, metadata):
        artifact = Artifact.from_text(
            artifact_id='root-projection:' + source + ':' + sha(content),
            kind=ArtifactKind.CONTROL_PLANE_EVIDENCE, origin=ArtifactOrigin.CONTROL_PLANE,
            source=source, content=content, media_type='application/json',
            created_at='1970-01-01T00:00:00+00:00', metadata=metadata)
        if self.store.has_artifact(artifact.artifact_id):
            require(self.store.get_artifact(artifact.artifact_id) == artifact, 'projection/provenance artifact changed')
        return self.spine._ensure(artifact)

    def project(self, *, goal_id, todo_id, objective, contract, dependency_refs, profile_id):
        require(type(self.spine.token_estimator) is Utf8TokenEstimator and
                self.spine.token_estimator == Utf8TokenEstimator(), 'estimator changed')
        require(profile_id in self.eligible_profiles,
                'not an eligible worker context profile')
        spec = self.spec_provider(goal_id)
        require(spec is not None and spec.goal_id == goal_id, 'trusted Mission missing')
        require(canonical_json(spec.to_dict()) == self.admitted_missions.get(goal_id), 'Mission binding changed')
        constraints = spec.to_dict()['global_constraints']
        require(constraints.get(VERIFIED_MANDATORY_CONTEXT_PROJECTION_V1) is True, 'policy not admitted')
        todo = self.todo_provider(goal_id)[todo_id]
        require(canonical_json(todo.work_contract.to_dict()) == canonical_json(contract.to_dict()),
                'durable Todo contract mismatch')
        require((contract.goal_id, contract.todo_id, contract.objective) == (goal_id, todo_id, objective),
                'Todo identity/objective mismatch')
        roots = self.spine.root_context(goal_id)
        source = self.artifact(roots.source, 'Phase16RootTaskSource', ArtifactOrigin.USER)
        capsule = self.artifact(roots.capsule, 'Phase16RootGoalCapsule', ArtifactOrigin.CONTROL_PLANE)
        expected_source = dict(schema_version=1, goal_id=goal_id, objective=spec.raw_objective,
                               constraints=constraints)
        expected_capsule = dict(schema_version=1, goal_id=goal_id, goal_capsule=spec.goal_capsule,
                                global_constraints=constraints, root_source_artifact_id=source.artifact_id,
                                root_source_sha256=source.sha256)
        require(source.artifact_id == 'phase16:root-source:' + stable_sha256(goal_id)[:20], 'source identity')
        require(capsule.artifact_id == 'phase16:root-capsule:' + stable_sha256(goal_id)[:20], 'capsule identity')
        require(source.content == canonical_json(expected_source), 'source differs from trusted Mission')
        require(capsule.content == canonical_json(expected_capsule), 'capsule differs from trusted Mission')
        full_contract = contract.to_dict()
        for key, value in constraints.items():
            require(key in full_contract['constraints'] and
                    canonical_json(value) == canonical_json(full_contract['constraints'][key]),
                    'contradictory or missing mandatory constraint: ' + key)
        refs = [ref.to_dict() for ref in dependency_refs]
        body = dict(schema_version=1, mission_id=spec.mission_id, goal_id=goal_id,
                    root_task_fingerprint=spec.root_task_sha256,
                    original_capsule_artifact_id=capsule.artifact_id, original_capsule_sha256=capsule.sha256,
                    current_todo_id=todo_id, current_todo_objective=objective,
                    context_profile_id=profile_id,
                    public_objective_summary=spec.goal_capsule,
                    required_dependency_summary={'artifact_refs_sha256': sha(canonical_json(refs)),
                                                 'todo_ids': list(todo.dependencies)},
                    required_worker_obligations='All obligations, SOURCE/TEST and forbidden scopes remain in the full REQUIRED WORK_CONTRACT; accepted dependency evidence is separately REQUIRED. Phase7 retains verification authority.',
                    projection_policy_version=POLICY)
        body['projection_fingerprint'] = sha(canonical_json(body))
        projection = VerifiedRootProjectionV1(canonical_json(body))
        metadata = {'goal_id': goal_id, 'todo_id': todo_id, 'phase16_role': 'VERIFIED_ROOT_PROJECTION'}
        ref = self.persist(projection.document, 'VerifiedRootProjectionV1', metadata)
        coverage = []
        for path, _ in leaves(expected_capsule):
            if path.startswith('/global_constraints/'):
                category, target = 'DUPLICATE_OF_VERIFIED_FIELD', '/constraints/' + path[len('/global_constraints/'):]
            elif path == '/goal_capsule':
                category, target = 'REQUIRED_MODEL_VISIBLE', '/public_objective_summary'
            else:
                category, target = 'CONTROL_PLANE_ENFORCED', 'trusted Mission and root source binding'
            coverage.append(dict(original_field=path, classification=category, target=target))
        proof = dict(schema_version=1, policy=POLICY, mission_id=spec.mission_id,
                     mission_sha256=sha(canonical_json(spec.to_dict())), original_source=roots.source.to_dict(),
                     original_capsule=roots.capsule.to_dict(), projection=ref.to_dict(),
                     work_contract_sha256=sha(canonical_json(full_contract)), dependencies=refs, coverage=coverage)
        self.persist(canonical_json(proof), 'RootProjectionProvenanceV1', metadata)
        return ref

    def record_pack(self, args, pack):
        text = self.spine.objective_with_pack(args['objective'], pack)
        proof = dict(schema_version=1, text_sha256=sha(text), args={
            k: args[k] for k in ('goal_id', 'todo_id', 'objective', 'profile_id')},
            recovery_artifacts=[ref.to_dict() for ref in args.get('recovery_artifacts', ())],
            required_artifact_ids=list(pack.required_artifact_ids),
            selected_artifact_ids=list(pack.selected_artifact_ids),
            context_fingerprint=pack.context_fingerprint)
        return self.persist(canonical_json(proof), 'RootProjectionDispatchV1',
                            {'goal_id': args['goal_id'], 'todo_id': args['todo_id']})

    def validate_dispatch(self, todo, profile_id, text):
        candidates = [self.store.get_artifact(ref.artifact_id) for ref in self.store.list_refs()]
        proofs = [item for item in candidates if item.source == 'RootProjectionDispatchV1'
                  and item.origin is ArtifactOrigin.CONTROL_PLANE and
                  decode_strict_json(item.content)['text_sha256'] == sha(text)]
        require(len(proofs) == 1, 'unrecognized dispatch text')
        proof = decode_strict_json(proofs[0].content)
        require(proof['args'] == dict(goal_id=todo.work_contract.goal_id, todo_id=todo.todo_id,
                                     objective=todo.objective, profile_id=profile_id), 'dispatch identity')
        optional = tuple(self.store.get_artifact(key).ref for key in proof['selected_artifact_ids']
                         if key not in proof['required_artifact_ids'])
        recovery = tuple(self.store.get_artifact(ref['artifact_id']).ref for ref in proof['recovery_artifacts'])
        require([ref.to_dict() for ref in recovery] == proof['recovery_artifacts'], 'recovery artifact changed')
        # Reconstruct through the normal spine: fresh dependency authority,
        # original root validation, estimator and actual budget checks all run.
        pack = self.spine.build_pack(**proof['args'], contract=todo.work_contract,
                                    dependency_todo_ids=todo.dependencies, optional_repo_artifacts=optional,
                                    recovery_artifacts=recovery)
        require(pack.context_fingerprint == proof['context_fingerprint'] and
                self.spine.objective_with_pack(todo.objective, pack) == text, 'stale context')
