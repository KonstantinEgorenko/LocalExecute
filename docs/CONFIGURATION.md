# Configuration

Use a trusted Git checkout as the control root. Keep the target repository and Mission state separate. Source defaults are under [config/](../config/). Keep credentials and machine-specific settings out of public changes.

| File | What it governs |
| --- | --- |
| [models.yaml](../config/models.yaml) | Logical roles, model tags, endpoint definitions and registry context defaults |
| [execution_profiles_v2.yaml](../config/execution_profiles_v2.yaml) | Profile-specific tag, context, generation bounds and tool allowlist |
| [routing.yaml](../config/routing.yaml), [router_v2.yaml](../config/router_v2.yaml) | Routing and escalation policy |
| [model_runtime.yaml](../config/model_runtime.yaml), [gpu_acceleration.yaml](../config/gpu_acceleration.yaml) | Placement, calibration, status and cleanup |
| [gpu_coordination.yaml](../config/gpu_coordination.yaml), [production_resources.yaml](../config/production_resources.yaml) | Lock coordination and production reserve policy inputs |
| [workflow.yaml](../config/workflow.yaml), [mission.yaml](../config/mission.yaml), [recovery.yaml](../config/recovery.yaml) | Durable workflow, completion and recovery bounds |
| [policies.yaml](../config/policies.yaml), [sandbox.yaml](../config/sandbox.yaml) | Workspace, verification and separate sandbox policy |

The production launcher overlays the physical Worker URL with the Lead URL. A measured runtime profile can overlay worker placement. Inspect the effective profile, context and model tags rather than relying on registry labels alone. Change one layer deliberately and recheck it. See [Ollama setup](OLLAMA_SETUP.md) and [resource management](RESOURCE_MANAGEMENT.md).

The semantic configuration files are authoritative. See the [migration guide](MIGRATION.md) for legacy configuration names and the supported descendant bootstrap.
