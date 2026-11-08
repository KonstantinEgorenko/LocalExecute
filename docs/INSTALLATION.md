# Install on Windows

Use Windows x64, CPython 3.12 and Git for the qualified setup. Ollama is needed for live model execution. Docker Desktop is a separate, optional dependency for the Docker isolation path. See [Docker setup](DOCKER_SETUP.md).

## 1. Install the host tools

Install CPython 3.12 for x64 from the [official Windows downloads](https://www.python.org/downloads/windows/). Install [Git for Windows](https://git-scm.com/install/windows). Open a new PowerShell session, then check the selected interpreter and Git:

```powershell
py -3.12 -c "import platform, struct; print(platform.python_version(), struct.calcsize('P') * 8)"
git --version
```

Expect `3.12.x 64` and a Git version. If the Python launcher is unavailable, use the full path to your verified CPython 3.12 executable for the venv creation step. Avoid mixing interpreters when installing dependencies.

## 2. Clone and install

```powershell
git clone https://github.com/KonstantinEgorenko/LocalExecute.git
Set-Location LocalExecute
$control = (Get-Location).Path
py -3.12 -m venv .venv
$python = Join-Path $control '.venv\Scripts\python.exe'
$cli = Join-Path $control '.venv\Scripts\localexecute.exe'
& $python -m pip install --require-hashes -r requirements/dev-win-py312.lock
& $python -m pip install --no-build-isolation --no-deps .
& $python -m pip check
& $cli --version
& $cli --help
& $python -m harness.mission_runner --help
```

The dev lock pins dependencies and build tools by hash. Use the commands in order, and stop if any command fails. The expected version for this source is `0.1.0`. The top-level help lists `run`; the Mission module help lists the Mission flags. A GitHub wheel can install Python modules, but the production control root still needs trusted Git-backed configuration. This walkthrough uses a clone for that reason.

For an offline install with a trusted wheelhouse containing every exact locked artifact, add `--no-index --find-links PATH_TO_WHEELHOUSE` to the hash-locked install. Replace the placeholder with a local directory. For hash errors, obtain the matching artifact and preserve the lock check.

## 3. Set up Ollama and the shared locks

Install the [native Windows Ollama application](https://ollama.com/download/windows). Follow the [Ollama guide](OLLAMA_SETUP.md) to align the configured Lead URL, exact model inventory and placement. Check the inventory without loading models.

The default lock directory is `C:/ProgramData/LocalExecute/locks`. Have the host owner provision it with trusted ownership and access control. When using an approved alternative, set both overrides in this PowerShell session. The following paths are examples for an already provisioned lock directory:

```powershell
$env:GPU_HEAVY_LOCK_PATH = 'C:\ProgramData\LocalExecute\locks\gpu_heavy.lock'
$env:MODEL_RUNTIME_LOCK_PATH = 'C:\ProgramData\LocalExecute\locks\model_runtime.lock'
```

Every cooperating process must use the same protected absolute paths. Ensure the operator can acquire the locks and unauthorized principals cannot replace the files or their parent. See [resource ownership](RESOURCE_MANAGEMENT.md).

## 4. Prepare a separate trusted target

Use a new, short, user-owned directory outside the control checkout. This illustrative fixture has no third-party dependencies. The Git author override is scoped to its initial commit.

```powershell
$base = Join-Path $env:LOCALAPPDATA ('lex-demo-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$target = Join-Path $base 'target'
$state = Join-Path $base 'state'
New-Item -ItemType Directory -Path $base | Out-Null
New-Item -ItemType Directory -Path $target, $state | Out-Null
git -C $target init
@'
def clamp_minimum(value, minimum):
    return min(value, minimum)
'@ | Set-Content -Encoding ascii -LiteralPath (Join-Path $target 'bounds.py')
git -C $target add -- bounds.py
git -C $target -c user.name='LocalExecute demo' -c user.email='demo@example.invalid' commit -m 'Add demo baseline'
git -C $target status --porcelain
$goal = 'Update clamp_minimum to return value when it is at least minimum and minimum otherwise. Preserve the function signature and add regression tests for below, equal and above the minimum.'
$mission = 'clamp-minimum-demo'
```

Expect no porcelain output. Keep the state directory outside the target. Use the same state directory, Mission ID and goal when intentionally inspecting or resuming the same Mission. Use a fresh state directory and identifier for a separate task.

## 5. Run the read-only preflight

```powershell
& $python -m harness.mission_runner --root $control --workspace $target `
  --state-root $state --mission-id $mission --profile production --goal $goal --preflight
```

This direct command checks Git separation and target cleanliness, state location, config and server discovery. It performs status discovery and does not create a Mission, worktree or loaded model. `READY` exits 0, `BLOCKED` exits 2, and a caught CLI failure exits 4. Inspect `issues` and the server observations, including `discovery_ok`. The command can report `READY` for a reachable endpoint whose status response failed decoding. `READY` does not certify model tags, locks, placement or startup calibration. See [what the launcher does](HOW_IT_WORKS.md#startup-and-ingress).

## 6. Optional operator-controlled production run

**Live example, syntax verified only.** Review the trust boundary, all effective models and resource checks first. Use an external supervisor appropriate for your host if you need an enforced process-tree deadline. The command below is the production launcher invocation; it does not create a separate outer supervisor.

```powershell
& $cli run --root $control --module harness.mission_runner -- `
  --root $control --workspace $target --state-root $state --mission-id $mission `
  --profile production --goal $goal --execute
```

The launcher runs startup reconciliation and may load models to calibrate worker profiles before Mission ingress. Do not use a forwarded help or preflight flag as a substitute for the direct read-only check. During execution, the worker can edit the candidate and configured native checks can run generated code. The report includes the Mission ID, status and durable state path. On completion it also includes the final report reference and summary. Review the retained candidate; integration is manual.

## Common failures

- **Wrong interpreter or missing executable.** Recheck the x64 Python 3.12 interpreter, reopen PowerShell after installation, and use the venv executables.
- **Dependency hash or missing wheel.** Verify architecture, lockfile, index or wheelhouse. Do not drop hash verification to make an install pass.
- **Git preflight blocked.** Confirm there is a commit, the target is clean and its Git common directory differs from the control checkout.
- **Ollama connection or model error.** Check the configured URL and full tag list in the [Ollama guide](OLLAMA_SETUP.md).
- **Worktree path errors.** Use short absolute paths outside the protected control checkout and check local permissions. See [troubleshooting](TROUBLESHOOTING.md).
