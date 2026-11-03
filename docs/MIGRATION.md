# Semantic layout migration

## Deliberate compatibility break

The physical path `harness/phase20v/bootstrap/sitecustomize.py` is retired for the next version. Use `harness/model_runtime/bootstrap/sitecustomize.py`. There is no hidden legacy directory or system-wide startup hook. Ordinary historical Python imports remain supported by an exact allowlisted adapter; they do not recreate filesystem paths.

Update descendant startup environments **before upgrading**:

```powershell
$control = (Resolve-Path "TRUSTED_CONTROL_CHECKOUT").Path
$bootstrap = Join-Path $control "harness/model_runtime/bootstrap"
$env:PYTHONPATH = $bootstrap + [IO.Path]::PathSeparator + $control
$env:SWARM_MODEL_RUNTIME = "1"
$env:SWARM_MODEL_RUNTIME_ROOT = $control
# Use your established protected, machine-wide files for all cooperating processes.
$env:GPU_HEAVY_LOCK_PATH = "EXISTING_SHARED_GPU_LOCK"
$env:MODEL_RUNTIME_LOCK_PATH = "EXISTING_SHARED_RUNTIME_LOCK"
& (Join-Path $control ".venv/Scripts/python.exe") -B -c "from harness.model_runtime import current_controller; assert current_controller() is not None"
```

The paths above are placeholders. Use absolute trusted paths and a provisioned environment. Run this setup in the owned descendant launcher environment, not as a global machine change. Python's normal `site` startup discovers canonical `sitecustomize` before user code; `-S` disables that mechanism and must not be used with this setup. Enabled model-runtime bootstrap installation failure retains exit code **88**. This check installs guards; it does not load models or qualify placement.

Literal deleted wrapper file paths and filesystem enumeration of old package directories are no longer supported. Use canonical module entrypoints. Existing ordinary import names, legacy class aliases, serialized V1 identifiers and environment-variable names are retained. Legacy CLI modules with established `python -m` entrypoints keep their behavior.

## Import and configuration readers

The closed compatibility mapping contains only reviewed historical targets. Leaf imports reuse the actual canonical module object; legacy package facades preserve their original exports and package behavior. Unknown legacy submodules fail rather than importing arbitrary candidate-selected code. Pickle compatibility means resolving existing historical class references; never load untrusted pickle data.

Semantic YAML files are authoritative. Historical filenames resolve to the semantic file when available; legacy-only user configuration remains readable. Conflicting legacy/current keys fail closed. All18 removed duplicate documents had equal normalized contents. Model budgets,profiles,timeouts and policy values are unchanged.

## Release policy

This branch does not replace v0.1.0 or publish a new release. Because the filesystem startup contract intentionally changes, recommend **v0.2.0** with this migration notice after independent acceptance, rather than silently shipping it as a fully backward-compatible patch. Persisted state field spellings are not renamed for branding.
