# Docker Desktop for optional isolated execution

Docker runs an image as a container with a separate filesystem and configurable process, network and resource boundaries. LocalExecute ships a Docker isolation primitive for explicitly configured sandbox workflows. Native Ollama on Windows can run without Docker. The default native Mission verification path can execute trusted code directly on the host.

## Install and check

1. Check Docker's current [Windows requirements and installer](https://docs.docker.com/desktop/setup/install/windows-install/). Enable supported hardware virtualization if required by the selected backend.
2. For Linux containers with WSL 2, use the supported Windows Subsystem for Linux 2 environment and select the WSL backend. Consult the [Docker WSL 2 guide](https://docs.docker.com/desktop/features/wsl/) for setup and alternative backend details.
3. Start Docker Desktop from the Start menu. Wait for the daemon before running:

```powershell
wsl --status
docker version
docker info
```

The first check applies when using WSL. `docker version` should show both Client and Server. A working CLI alone does not demonstrate daemon access. These are vendor status commands; no container is started by them.

## What isolation means here

| Control | Purpose |
| --- | --- |
| Restricted mounts | Expose only the inputs and writable outputs a job needs |
| Network isolation | Disable container network access for jobs that do not need it |
| Read-only root filesystem | Limit mutation of the image filesystem |
| Dropped capabilities and privilege restrictions | Reduce the privileges available to the container process |
| Resource limits | Bound memory, CPU, process count and execution time as applicable |

The actual launch rules are in the [sandbox policy](../config/sandbox.yaml) and [Docker CLI adapter](../harness/sandbox/docker_cli.py). A Git worktree separates candidate files and revisions. A container can restrict the executing process. Treat these as different boundaries. Container isolation also depends on the host, daemon, image and mounts. Avoid privileged containers, broad host mounts and exposure of the Docker socket.

## Troubleshooting

If the daemon is unavailable, check Docker Desktop startup and the selected context. For WSL backend failures, check virtualization and the vendor's WSL prerequisites. For image or mount failures, inspect the configured image identity, restricted path access and the sandbox report. For PID or memory failures, retain the actual daemon error and review the applicable limits before changing policy. Docker readiness does not demonstrate Ollama or Mission readiness.
