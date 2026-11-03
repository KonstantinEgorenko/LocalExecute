# Architecture

Canonical implementations separate control, model execution and acceptance. Native execution requires trusted targets.

```mermaid
flowchart LR
 CP[Mission control plane] --> P[Persistent state]
 CP --> RT[Model runtime]
 RT --> O[Local Ollama]
 RT --> L[Shared resource ownership]
 CP --> T[Authorized tools]
 T --> W[Candidate worktree]
 W --> V[Verification and acceptance]
 V --> P
 CP -. optional .-> Q[Research providers]
```

## Local model execution

```mermaid
flowchart TD
 Roles[Logical Lead Worker Critic Judge] --> R[Resource-aware routing]
 R --> L[MODEL_RUNTIME and GPU_HEAVY ownership]
 L --> S[Startup inventory-wide calibration]
 S --> C[Durable measured profile cache]
 C --> A[Memory admission and load]
 A --> O[One physical Ollama endpoint]
 O --> U[Unload or bounded cleanup]
```

Calibration currently covers effective Worker inventory before Mission dispatch; demand-driven initialization is not implemented. The model-only policy constrains resident allocation to11264MiB and requires16384MiB available RAM. It does not prove a hard transient peak cap.

## Acceptance boundary

```mermaid
flowchart LR
 G[Generated untrusted candidate data] --> V[Trusted deterministic verification]
 V --> C[Advisory Critic]
 C --> J[Judge with bound evidence]
 J --> F[Completion invariants]
 F --> A[Accepted artifact]
 A -. separate scoring .-> E[Independent posthoc checks]
 V -->|failure blocks acceptance| B[Recovery or blocked]
```

## Durable lifecycle

[Persisted Mission lifecycle diagram](architecture/mission-lifecycle.md).
