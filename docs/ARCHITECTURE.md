# Architecture

LocalExecute separates durable control, physical model execution, candidate mutation and acceptance. This guide describes the public v0.1.0 source layout, where semantic packages and some historical paths coexist.

```mermaid
flowchart TD
    subgraph Control["LocalExecute control plane"]
        Mission["Mission controller"] --> Flow["Workflow and scheduler"]
        Flow --> Route["Routing and executor"]
        Flow --> Verify["Verification and review"]
        Mission <--> State["Durable state and artifacts"]
        Verify --> State
    end
    subgraph Model["Model runtime boundary"]
        Owner["Lifecycle and OS locks"] --> Server["Local Ollama API"]
    end
    subgraph Candidate["Candidate boundary"]
        Tools["Scoped workspace tools"] --> Worktree["Owned Git worktree"]
    end
    Route --> Owner
    Route --> Tools
    Worktree --> Verify
    Verify --> Owner
    Flow -. optional .-> Research["Research services"]
```

| Area | Source entry points |
| --- | --- |
| Ingress and composition | [CLI](../localexecute/cli.py), [Mission runner](../harness/mission_runner.py), [production factory](../harness/integration/runtime_factory.py) |
| Durable control | [Mission controller](../harness/mission/controller.py), [workflow controller](../harness/workflow/controller.py), [scheduler](../harness/scheduler/) |
| Routing and execution | [Router](../harness/router/), [executor](../harness/executor/), [model runtime](../harness/model_runtime/) |
| Worktree and tools | [Workspace](../harness/workspace/), [worktree manager](../harness/worktree/manager.py) |
| Acceptance and recovery | [Verification](../harness/verification/), [Critic](../harness/critic/), [Judge](../harness/judge/), [recovery](../harness/recovery/) |

## Mission lifecycle

These edges summarize persisted transitions in the [Mission controller](../harness/mission/controller.py). A repair within a Todo need not change the Mission to `RECOVERING`. Mandatory evidence is revalidated on a completed restart.

```mermaid
stateDiagram-v2
    [*] --> CREATED
    CREATED --> PLANNED: accepted plan
    CREATED --> FAILED: planning contract failure
    PLANNED --> RUNNING: workflow starts
    RUNNING --> RUNNING: workflow checkpoint or restart
    RUNNING --> RECOVERING: admissible amendment
    RECOVERING --> RUNNING: workflow reentry
    RUNNING --> VERIFYING: workflow succeeded
    VERIFYING --> COMPLETED: completion gate passed
    VERIFYING --> BLOCKED: completion gate failed
    RUNNING --> BLOCKED: no admissible continuation
    RUNNING --> FAILED: workflow boundary failure
    BLOCKED --> RUNNING: later invocation within policy
    COMPLETED --> BLOCKED: mandatory evidence invalidated
```

## Windows setup boundary

```mermaid
flowchart TD
    subgraph Windows["Windows x64 host"]
        PS["PowerShell"] --> Python["Python 3.12 and LocalExecute"]
        Python --> Git["Git and trusted target"]
        Python -->|Live only, loopback HTTP| Ollama["Native Ollama"]
        Ollama --> Weights["Local model weights"]
        Python -. explicitly configured sandbox .-> Docker["Optional Docker Desktop"]
        Docker -. WSL backend option .-> WSL["WSL 2"]
    end
```

See [installation](INSTALLATION.md), [Ollama](OLLAMA_SETUP.md) and [Docker](DOCKER_SETUP.md).
