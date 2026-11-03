# Validation

Baseline v0.1.0 public suite:428 passed,2 intentional Windows symlink privilege skips;430 collected. Development evidence:4532 passed,2 skipped. These counts are historical qualification, not a new branch run.

Offline native SIMPLE uses deterministic physical model responses and real runtime components. Its test substitutes Mission ingress rather than exercising full mission_runner argument parsing. REPLAN qualification includes durable transfer and interruption checks, not a live-model REPLAN Mission.

Live attempt1: preflight blocked; attempt2: supervisor stopped startup calibration at949MiB actual-free under an additional1536MiB experiment guard. Mission not created,token generations0. Exact in-flight model attribution remained unverified. No model-specific OOM or broad task reliability is claimed.

Branch-specific compatibility,installation,rendering and regression results are recorded in the pull request. The startup-path retirement is explicit, not an unresolved promise of filesystem compatibility. No live tests are part of ordinary offline pytest.
