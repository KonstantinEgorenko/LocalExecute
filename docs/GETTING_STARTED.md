# Getting started

Use Windows x64/CPython3.12 and the hash-locked development environment. The README commands match the existing CLI parser. A source archive needs a separately initialized trusted Git control checkout. Target and state roots must be separate from protected control paths. Provision shared lock ACLs explicitly or use the existing GPU_HEAVY_LOCK_PATH and MODEL_RUNTIME_LOCK_PATH environment overrides consistently across applications.

Direct mission_runner --preflight checks Git/server/config readiness without placement. The production launcher can load models during startup even when its forwarded module asks for preflight. --execute calls real configured models and must be treated as host-code execution.
