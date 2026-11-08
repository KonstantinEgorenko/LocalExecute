# Troubleshooting

| Symptom | Check first |
| --- | --- |
| Missing Python launcher, Git or venv executable | Reopen PowerShell after installation. Verify Windows x64 CPython 3.12 and invoke the venv executable by its explicit path |
| Hash-locked install fails | Confirm the Python architecture and exact lock artifact. Verify the wheelhouse or package index, and keep hash checks enabled |
| Preflight is `BLOCKED` | Read `issues`. Verify the control root is Git-backed, the target is a separate clean checkout and the state directory is outside the target |
| Endpoint refused or model missing | Check the Lead URL, `/api/tags` and full model tags. Inspect `TODO` resolution and profile bindings |
| Direct preflight is `READY`, launcher still blocks | Inspect startup reconciliation, inventory profile calibration, RAM, GPU residency, lock availability and journal blockers |
| GPU lock busy or unexpected residency | Identify the current owner through runtime evidence. Coordinate with the host owner before attempting another run |
| Native verification fails | Read the first deterministic failure and confirm the report belongs to the candidate revision under review |
| Verification passes but the Mission is blocked | Inspect Critic and Judge evidence, mandatory obligations, final/global gates and completion evidence |
| Docker CLI installed, daemon unavailable | Start Docker Desktop and check daemon/context and the selected virtualization backend |
| Long path or access error | Use a short owned state and target path, outside the control checkout, with the appropriate permissions |

Start with [installation](INSTALLATION.md), [Ollama setup](OLLAMA_SETUP.md), [Docker setup](DOCKER_SETUP.md) or [resource management](RESOURCE_MANAGEMENT.md). For a report include the source revision, Python/Windows versions, command shape, exit/status, first causal error and sanitized evidence.
