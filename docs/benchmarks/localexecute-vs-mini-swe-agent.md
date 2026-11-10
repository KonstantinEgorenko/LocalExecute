# LocalExecute vs mini-SWE-agent: controlled staging benchmark

**Experiment date:** 2026-11-10. **Scope:** eight frozen single-file Python bug-fix tasks, one physical attempt per task and mode. This document reports an *isolated staging experiment*, not a performance qualification of the public LocalExecute v0.1.0 release or a standardized SWE-bench score.

## Executive result

LocalExecute Worker v0.1 completed **3/8** tasks (37.5%). mini-SWE-agent completed **0/8** in both the four-generation **equal** mode and the eight-generation **native** mode. For mini-SWE-agent, one *unsubmitted but independently validated* patch occurred in each mode (`boundary-seat`). The respective numbers of independent test-valid patches were **3/8, 1/8 and 1/8**. All runs had confirmed model cleanup; both mini-SWE modes additionally had verified Docker-container cleanup.

**Interpretation:** on this narrowly scoped set, the LocalExecute staging worker achieved more completed bug fixes with fewer generations and generated tokens. The systems use materially different action interfaces, prompts and test-feedback surfaces. These measurements do **not** establish that LocalExecute is generally a better coding agent or that the model is faster at token decoding.

## Experimental controls and agent differences

| Setting | LocalExecute v0.1 | mini-SWE equal | mini-SWE native |
| --- | --- | --- | --- |
| Model | `deepseek-coder-v2:16b` | Same | Same |
| Model digest (SHA-256) | `63fb193b3a9b4322a18e8c6b250ca2e70a5ff531e962dbf95ba089b2566f2fa5` | Same | Same |
| Model service | Local Ollama `/api/chat` | Journaled Ollama `/api/chat` | Journaled Ollama `/api/chat` |
| Context / temperature / max generated tokens per response | 4,096 / 0 / 768 | Same | Same |
| GPU layers requested | `num_gpu=1` | Same | Same |
| Maximum model generations per task | 4 | 4 | 8 |
| Agent protocol | `READ_TEXT → WRITE_TEXT → FINAL`, SHA-guarded CAS | Upstream bash action parser + custom journaled transport | Same as equal |
| Candidate validation | Regression + independent oracle before write, then after | Regression + independent oracle after isolated agent run | Same as equal |
| Edit scope | One existing source line in one Python file | One source file in an isolated, networkless Docker workspace | Same as equal |
| mini-SWE upstream version | n/a | v2.4.6, commit `a83fcae82d2a08f0ee0c688f9d137b3566c097f8` | Same |

The mini-SWE runner retains upstream `DefaultAgent`, `mini_textbased.yaml` and `mswea_bash_command` parsing, but uses a custom one-shot journaled Ollama transport and a hardened Docker workspace. It is therefore **not an unmodified end-to-end upstream deployment**. The same frozen fixtures were copied into fresh mini-SWE workspaces. Each system used separate operator authorization. The native mode has twice the allowed generations, so its higher resource consumption is not an equal-budget result.

The eight task families were: two boundary conditions (`boundary-window`, `boundary-seat`), two boolean-logic cases (`logic-export`, `logic-signal`), two arithmetic cases (`arithmetic-split`, `arithmetic-wrap`) and two collection-processing cases (`collection-order`, `collection-count`). Each initial fixture failed both its regression and independent oracle. Every task admitted a one-line correct fix, and each test pair was frozen before live runs.

## Overall results

| Metric | LocalExecute v0.1 | mini-SWE equal | mini-SWE native |
| --- | ---: | ---: | ---: |
| Planned / attempted | 8 / 8 | 8 / 8 | 8 / 8 |
| **E2E complete + independently verified** | **3 / 8** | **0 / 8** | **0 / 8** |
| Valid final patch (including no formal submission) | **3 / 8** | **1 / 8** | **1 / 8** |
| Actual generations | 25 | 32 | 57 |
| Generated/output tokens | **3,029** | 5,587 | 8,588 |
| Prompt/input tokens | 19,616 | 34,747 | 81,127 |
| Tool / Bash actions recorded | 22 | 31 | 55 |
| Total server inference duration | **213.41 s** | 387.51 s | 668.36 s |
| Median inference duration per task | **27.52 s** | 41.31 s | 88.20 s |
| Effective output rate [A] | 14.19 tok/s | 14.42 tok/s | 12.85 tok/s |
| Aggregated decode rate [B] | 18.00 tok/s | 16.23 tok/s | 14.58 tok/s |
| Median task E2E wall-clock | Not instrumented individually | 58.13 s | 108.52 s |
| Campaign wall-clock | 373.15 s | Not consolidated | 888.04 s |
| Physical POST (preload + generation + unload) | 41 (8 + 25 + 8) | 48 (8 + 32 + 8) | 73 (8 + 57 + 8) |
| Verified owned model unloads | 8 / 8 | 8 / 8 | 8 / 8 |
| Verified Docker cleanups | Different execution path | 8 / 8 | 8 / 8 |
| New unresolved physical dispatches | 0 | 0 | 0 |

[A] **Effective output rate** is total output tokens divided by total Ollama-reported model inference time. This includes prompt evaluation, not just decoding. A higher value is *not* evidence of a higher model decoding rate.

[B] Decode rate divides the sum of output tokens by the sum of server-reported decode durations for all generation receipts. It excludes prompt evaluation.

The LocalExecute campaign generated about **1.01k output tokens per completed E2E across all eight tasks**, and **71.14 server inference seconds per completed E2E**, calculated as 3,029/3 and 213.4115/3; this includes tokens consumed by five failed tasks and is not a per-successful-trajectory mean. There are no completed E2E successes in either mini-SWE mode, so an analogous cost per completed task is undefined (not zero). The mini-SWE equal and native campaigns consumed approximately **1.84×** and **2.84×** as many output tokens as LocalExecute, respectively; those ratios also reflect differences in completed actions and allowed generations.

## Paired task outcomes

| Task | LocalExecute (4 gen max) | mini-SWE equal (4 gen max) | mini-SWE native (8 gen max) |
| --- | --- | --- | --- |
| `boundary-window` | **PASS** | FAIL, file unchanged | FAIL, submitted with no change |
| `boundary-seat` | FAIL, two semantic no-ops | FAIL, **valid patch not submitted** | FAIL, **valid patch not submitted** |
| `logic-export` | FAIL, no-op followed by failed regression | FAIL, tests failed | FAIL, submitted invalid patch |
| `logic-signal` | **PASS** after correction | FAIL, file unchanged | FAIL, submitted with no change |
| `arithmetic-split` | FAIL, two semantic no-ops | FAIL, file unchanged | FAIL, budget exhausted without edit |
| `arithmetic-wrap` | FAIL, two semantic no-ops | FAIL, tests failed | FAIL, invalid patch, budget exhausted |
| `collection-order` | **PASS** | FAIL, unchanged; one format error | FAIL, unchanged; one format error |
| `collection-count` | FAIL, two edits rejected by one-line policy | FAIL, file unchanged | FAIL, unchanged; one format error |

`boundary-seat` is the clearest example of different failure criteria: mini-SWE changed the file and passed both regression and oracle, but the final mandatory submission command never arrived before the generation limit. Counting the patch as E2E success would change the pre-registered completion metric; the report instead shows *patch validity* separately.

## mini-SWE native: per-task resource and speed evidence

The numbers below are the **minimum available host RAM** and **minimum free VRAM** observed at journal checkpoints, not memory consumption by the agent or peak allocated GPU memory.

| Task | Gen / Bash | Input / output tokens | Server inference / task E2E (s) | Decode (tok/s) | Minimum available RAM / free VRAM (MiB) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `boundary-window` | 5 / 5 | 7,125 / 1,270 | 94.98 / 111.64 | 14.59 | 30,134 / 13,246 |
| `boundary-seat` | 8 / 8 | 10,770 / 819 | 65.01 / 88.59 | 15.23 | 29,975 / 13,194 |
| `logic-export` | 6 / 6 | 6,620 / 572 | 41.91 / 62.75 | 16.39 | 30,004 / 13,235 |
| `logic-signal` | 6 / 6 | 7,848 / 924 | 70.54 / 89.84 | 14.91 | 30,007 / 13,221 |
| `arithmetic-split` | 8 / 8 | 10,972 / 1,344 | 105.21 / 127.08 | 14.24 | 30,594 / 13,299 |
| `arithmetic-wrap` | 8 / 8 | 11,696 / 1,002 | 81.43 / 105.41 | 14.22 | 30,328 / 13,123 |
| `collection-order` | 8 / 7 | 12,403 / 1,364 | 106.24 / 128.48 | 14.32 | 30,345 / 13,125 |
| `collection-count` | 8 / 7 | 13,693 / 1,293 | 103.05 / 124.53 | 14.18 | 30,636 / 13,109 |

Across the native series, the minimum of reported available RAM was **29,975 MiB**, and the minimum observed free VRAM was **13,109 MiB**. The highest model `size_vram` reported by Ollama was **334,443,314 bytes (318.95 MiB)** in each native attempt. This value describes the reported resident model share and **must not be interpreted as total GPU memory usage or peak VRAM allocation**. Actual GPU peak allocation, CPU/GPU utilization, energy consumption and power draw were **not measured**. LocalExecute's summarized runtime snapshots showed a minimum available RAM of **29,484 MiB** and free VRAM of **13,409 MiB**; equal-mode runtime snapshots showed minima of **31,087 MiB** available RAM and **13,370 MiB** free VRAM. These free-memory minima should not be presented as direct or isolated per-agent RAM/VRAM usage deltas because other host processes may affect them.

The requested `num_gpu=1` and small observed model GPU residency indicate limited GPU offload, so inference timing is **not** a measure of maximum NVIDIA GPU inference throughput. Up to the stated sample/measurement precision, equal-mode output-only performance and LocalExecute output-only performance were similar, while their step/token consumption differed.

## Failure analysis and safety

LocalExecute's five failures were independently classified from saved model responses as three double semantic no-ops, one no-op followed by a regression rejection, and one case with two proposals violating the single-line mutation policy. Those were **not** agent-action JSON parsing errors. In mini-SWE, several runs spent the available budget exploring or self-testing without producing an accepted candidate; in native mode, some submitted unchanged or incorrect files. Thus E2E failures can involve both model decision-making and harness/prompt scheduling, and the observed difference cannot be ascribed to one factor alone.

Every series verified 8/8 owned model unloads and ended with no new unresolved physical operations. The mini-SWE runner verified 8/8 Docker container removals in both modes. The evidence does not demonstrate identical safety properties: LocalExecute used pre-write candidate gates and compare-and-swap, while mini-SWE used sandboxed Bash with a post-run evaluator. A shared Ollama listener and incomplete GPU placement peak monitoring remain limitations of this staging environment.

## Provenance and limits

- LocalExecute Worker freeze SHA-256: `f55b6e6b39ceaea590c791b0d827ebea8f47e6e87a91d67a0d1e5f1b434ca4f4`.
- Pinned mini-SWE upstream: [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent/tree/a83fcae82d2a08f0ee0c688f9d137b3566c097f8), package v2.4.6.
- LocalExecute source report SHA-256 (`stdout.json`): `c98e050d44bc7c0f695d21ea37e83db79a8a5ea802524185f3fca52401e1fc8f`.
- mini-SWE equal paired results SHA-256 (`comparison.utf8.md`): `3dbb5aa6439e94cc45eff56c0d329c65ba9df32b821c551c31cd9ebf115bef1b`.
- mini-SWE native source summary SHA-256 (`summary.json` as supplied): `04af2ae7bfc638ee744adeeec7eee8d5d58671e46573e3eab4b9065166668b05`.
- Full raw physical journals, model responses, trajectories, fixtures and independent tests were retained in an isolated operator-controlled staging environment; they are **not** committed to this public report. Without those source artifacts and the benchmark code, independent reproduction and full audit of individual trajectories is limited.
- Only **eight small purpose-built single-file defects**, one attempt per task per mode, were tested. No uncertainty intervals, repeatability estimates, multiple random seeds, multi-file coding tasks or production load tests were performed. Results are descriptive, not statistically significant proof of overall agent superiority.
- Hardware and host background-load parity were not measured exhaustively. Model, digest, context, temperature and response caps were fixed, but agent prompts, tool interfaces, environment and test feedback were different by design.

**Result:** the tested LocalExecute staging Worker configuration completed more of these restricted tasks at lower campaign-level inference and token cost. The next step for broader external validity would be larger, publicly reproducible, frozen tasks and repeated paired trials—not extrapolating this eight-case sample to general software engineering performance.
