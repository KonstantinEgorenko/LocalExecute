# Set up native Ollama

Ollama serves local model weights through an HTTP API. LocalExecute uses it for planning, coding and review. The harness sends model requests and applies its own role, placement, tool and acceptance policies. It does not ship weights.

## 1. Install and locate the server

Use the [manual Windows installer](https://ollama.com/download/windows). See the [Ollama quickstart](https://docs.ollama.com/quickstart) and [API reference](https://docs.ollama.com/api). The standard local port is `11434`. This repository's production Lead URL defaults to `http://127.0.0.1:11435`. Either point the trusted control configuration at your existing local server or deliberately configure a loopback-only Ollama listener at the repository port.

For a manually managed server on an unused port, the following is an operator example. Run it in a dedicated PowerShell session with Ollama installed. Keep it running while using a separate shell for status requests. Use your existing server instead if it already owns that endpoint.

```powershell
$env:OLLAMA_HOST = '127.0.0.1:11435'
ollama serve
```

Keep the `ollama_base_url` and `openai_base_url` in the Lead entry of [models.yaml](../config/models.yaml) consistent, with `/v1` only on the latter. The production overlay maps the logical Worker server onto the Lead server for that process. The separate Worker URL in the static YAML is not a second production server requirement. A non-production integration may use the static topology.

## 2. Check inventory and residency

In the operator shell, set the base URL to the effective endpoint:

```powershell
$ollama = 'http://127.0.0.1:11435'
(Invoke-RestMethod -Uri "$ollama/api/tags" -Method Get).models |
  Select-Object name, digest
(Invoke-RestMethod -Uri "$ollama/api/ps" -Method Get).models |
  Select-Object name, digest, size_vram, context_length
```

`/api/tags` lists installed models. `/api/ps` lists models currently loaded. An empty resident list is compatible with a populated inventory. Response fields depend on the backend version. To target the same server with the Ollama CLI, set its process-local address first:

```powershell
$env:OLLAMA_HOST = $ollama
ollama list
ollama ps
```

These are status commands. They do not pull or generate. The vendor commands and HTTP syntax are documented here; this documentation update does not exercise a live endpoint.

## 3. Resolve logical IDs to actual tags

| Logical ID in the default registry | Tag or resolution input |
| --- | --- |
| `ornith_lead` | `hf.co/deepreinforce-ai/Ornith-1.0-35B-GGUF:Q4_K_M` |
| `ministral_reasoner` | `ministral-3:14b` |
| `deepseek_fast_coder` | `TODO`. Runtime inventory rules look for DeepSeek Coder V2 with Lite or 16B tokens |
| `iquest_repo_coder` | `TODO`. Runtime inventory rules look for IQuest 14B |
| `qwen35_heavy_fast` | `qwen36-35b-a3b-heavy-fast:latest` |
| `qwen27_heavy_quality` | `qwen36-27b-heavy-quality:latest` |

1. Match the full tag and record its digest from the effective server's inventory. Display names and logical IDs can differ from the physical tag. Local aliases must already exist in your server inventory.
2. For each `TODO`, inspect the [runtime resolution rules](../config/model_runtime.yaml) and confirm the selected inventory tag, or deliberately configure an exact installed tag in your trusted registry. Missing or ambiguous inventory needs operator review.
3. Check every effective worker involved in startup and every enabled route, including any profile-specific binding in [execution_profiles_v2.yaml](../config/execution_profiles_v2.yaml). The profile catalog and registry are distinct. Entries or calibration references do not independently qualify a model.
4. Check that each selected model can follow the required structured output and tool protocol, and fits the configured context and placement. One small local model does not satisfy the entire multi-role configuration.
5. If weights are missing, review the model's terms and size before explicitly choosing to import or download it using the vendor's instructions. This is an optional operator action. No setup check should download weights implicitly.

## 4. Diagnose live readiness

- **Connection refused.** Confirm the listener is running at the exact Lead host and port. An API responding on 11434 does not validate a configuration targeting 11435.
- **`MODEL_NOT_INSTALLED`.** Compare the requested full tag and endpoint to `/api/tags`. Review `TODO` resolution and profile bindings. Do not substitute a nearby tag without reviewing compatibility.
- **Resource admission or calibration failure.** Inspect resident models, effective context, GPU layer placement and available system RAM. Offloaded weights consume RAM, and a longer context can increase backend memory demand. Multiple large residents can exceed the budget even when each fits alone. See [resource management](RESOURCE_MANAGEMENT.md).
