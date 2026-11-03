# How it works

Mission ingress preserves a task specification. Lead planning is compiled and checked; routing and workspace authority stay outside model discretion. Tools produce candidates, never acceptance. Fresh verification, strict review and completion invariants bind the accepted artifact. Durable recovery contexts do not substitute stale verification.

```mermaid
flowchart TD
 U[User task] --> CLI[Public CLI]
 CLI --> M[Durable Mission and Lead plan]
 M --> R[Resource-aware Router]
 R --> W[Worker and authorized tools]
 W --> G[Candidate Git worktree]
 G --> V[Fresh deterministic verification]
 V -->|pass| C[Advisory Critic]
 C --> J[Strict Judge]
 J -->|ACCEPT plus all completion invariants| A[Accepted artifact and durable completion]
 V -->|failure| B[Bounded recovery]
 J -->|REPLAN or rejection| B
 B -->|authorized revision| M
 B -->|limits or missing authority| X[Blocked or failed]
```
