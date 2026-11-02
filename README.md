# LocalExecute — Autonomous Agent Harness

Created by Konstantin Egorenko. Apache-2.0; see LICENSE and NOTICE.

Experimental local-model coding harness. Production Mission planning, rule/score routing,
isolated Git worktrees, bounded workers, deterministic verification, advisory Critic,
strict Judge acceptance and durable completion share one production pipeline.

## Qualified release scope

Windows x64, CPython 3.12. Offline deterministic physical model transport tests qualify
native SIMPLE discovery/coding and REPLAN draft recovery. Git and Docker Desktop are
external prerequisites. This is not a claim of universal live-model reliability,
Linux qualification or compatibility with every Python version allowed by metadata.

## Install and test

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements/dev-win-py312.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps .
.\.venv\Scripts\localexecute.exe --help
.\.venv\Scripts\python.exe -m pytest -q
```

Use a short temporary directory outside this checkout for tests requiring managed
worktrees. Native ownership tests pin the real Python base image, preserving Windows
Job identity checks when the calling interpreter is a standard virtual environment.
Tests use synthetic backend responses; they never download models or invoke Ollama.

## Runtime prerequisites

Configure your own separately licensed local models and Ollama endpoints before real
Mission execution. `localexecute run --help` documents the existing production entry.
Example configs use loopback endpoints and one absolute machine-wide lock namespace
(`C:/ProgramData/LocalExecute/locks`). Provision that directory with appropriate
owner/ACL policy, or set existing `GPU_HEAVY_LOCK_PATH` and
`MODEL_RUNTIME_LOCK_PATH` to shared protected absolute files for your machine.
All projects/roles must use the same namespace; never use cwd-relative locks. One GPU
worker generates at a time. Do not run competing heavy generations. Keep resource
limits, workspace authority and deterministic verification enabled.

## Production invocation

The `--root` control/config checkout must be trusted and Git-backed; a normal
clone of this clean public source has that identity. An unpacked source archive
needs a separately initialized trusted Git configuration checkout. Do not use
the target repository as the control root. Keep managed target worktrees outside
the protected control checkout. Prepare models, endpoints and shared lock ACLs
before executing; the following is an operator example, not an offline test:

```powershell
localexecute run --root "TRUSTED_CONTROL_CHECKOUT" --module harness.mission_runner -- `
  --root "TRUSTED_CONTROL_CHECKOUT" --workspace "SEPARATE_TARGET_CHECKOUT" `
  --state-root "OWNED_STATE_DIRECTORY" --profile production `
  --goal "Update the implementation and add regression tests for the public contract." --execute
```

Replace the three uppercase path placeholders with actual absolute paths. `--execute` calls
real configured models; it was not run during release qualification. Inspect
`python -m harness.mission_runner --help` for the existing ingress contract.

## Acceptance and independent evaluation

Public Mission completion requires configured fresh verification, exact accepted
candidate evidence, a valid Judge ACCEPT, clean worktrees and durable state. Generated
tests are not independent ground truth. The private benchmark-specific V1 independent
evaluator is not distributed. A Mission explicitly requiring it fails closed when
unavailable, including terminal restart. It is never replaced by success.
Optional posthoc scoring is outside Mission completion: consume only validated
accepted candidate exports with commit/tree/content identities and provenance.
Do not import private benchmarks into the installed runtime or use scores to forge
verification/Judge receipts.

## Trusted-code execution boundary

Native Phase7 verification executes candidate code on the host and requires
trusted worktrees. Git worktree/path authorization and Windows Job ownership
are not an OS security sandbox. Native v0.1 is for trusted first-party target
code and controlled local use; malicious/untrusted repository or generated code
is outside that native security claim. The Docker isolation primitive is
qualified separately. Do not assume the production CLI automatically routes
every verification command through Docker; that universal routing is not claimed.

## Limits

No hidden oracle, private history, raw experiment logs, model weights or developer
environments are included. Research URL/security/store primitives are tested offline;
external search/browser/PDF/office/media providers and live services are unqualified
optional features in this release. No npm/OpenCode toolchain qualification is claimed.
Package naming observations do not reserve registry names. Owner final review of the
source inventory and contributor rights is required before publication.
