# Security and trust boundaries

Use native execution only with trusted targets, trusted control configuration and an operator willing to run generated code under the host account. The worker's scoped file operations constrain edits inside the candidate workspace. Native verification can execute candidate code with host permissions.

| Boundary | What it provides |
| --- | --- |
| Workspace authorization | Scope checks on tool operations and canonical paths |
| Git worktree | Candidate checkout and revision isolation |
| Windows Job ownership where used | Ownership and control of a supervised process tree |
| Docker sandbox primitive | A separately configured execution boundary with a specific mount, network and resource policy |
| Verification and Judge | Evidence-based acceptance for configured contracts |

Worktree isolation and Windows process ownership do not provide a malicious-code sandbox. Docker is not automatically enforced by the production CLI. Review the [Docker setup](DOCKER_SETUP.md) and actual [sandbox policy](../config/sandbox.yaml) when relying on that boundary.

Keep the control checkout separate from the target. Keep Ollama on an appropriate local interface, and protect shared lock paths. Inspect target scripts and test commands before execution. Review a retained candidate before manual integration. Keep logs and state local unless reviewed for source, prompts, paths and secrets.

For security reports use the repository's [private vulnerability reporting channel if enabled](https://github.com/KonstantinEgorenko/LocalExecute/security). If it is unavailable, request a private contact route without including sensitive details in a public issue. For public bug reports see [contributing](CONTRIBUTING.md).
