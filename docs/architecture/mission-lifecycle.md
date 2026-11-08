# Durable Mission lifecycle

These edges reflect persisted transitions in `harness/mission/controller.py`. Recovery within a Todo is a separate workflow concern; not every retry creates a Mission RECOVERING transition. COMPLETED can become BLOCKED on mandatory acceptance revalidation. FAILED remains terminal in this controller.

```mermaid
stateDiagram-v2
 [*] --> CREATED
 CREATED --> PLANNED: accepted plan
 CREATED --> FAILED: planning contract failure
 PLANNED --> RUNNING: workflow invocation
 RUNNING --> RUNNING: restart or invocation checkpoint
 RUNNING --> VERIFYING: workflow succeeded
 VERIFYING --> COMPLETED: all global completion invariants
 VERIFYING --> BLOCKED: completion gate failed
 RUNNING --> RECOVERING: bounded DAG amendment accepted
 RECOVERING --> RUNNING: amendment applied and workflow reentry
 RUNNING --> BLOCKED: no admissible continuation or budget exhausted
 RUNNING --> FAILED: workflow boundary exception
 BLOCKED --> RUNNING: explicit later invocation within policy
 COMPLETED --> BLOCKED: terminal acceptance invalidated
```
