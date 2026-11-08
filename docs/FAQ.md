# Frequently asked questions

**What does LocalExecute do?** It turns a coding objective into bounded local-model work and checks a candidate through deterministic verification and structured acceptance. See [how it works](HOW_IT_WORKS.md).

**Do I need Docker or WSL 2?** Native Ollama and the native Mission path can run on Windows without them. Docker is for separately configured sandbox work. WSL 2 applies when using Docker Desktop's WSL backend. See [Docker setup](DOCKER_SETUP.md).

**Do I need Ollama and which models should I install?** For real inference, use a reachable Ollama server and inventory compatible with the active registry and execution profiles. Installing dependencies or checking CLI help needs no model weights. See [Ollama setup](OLLAMA_SETUP.md).

**Can I run this on my GPU?** Fit depends on effective profiles, context, quantization, available RAM and backend placement. See [resource management](RESOURCE_MANAGEMENT.md).

**Does `READY` mean a live Mission will run?** It means the direct preflight checks passed. Startup reconciliation, model tag resolution, calibration and placement are later checks.

**Will LocalExecute merge changes?** Accepted candidates are retained for review and manual integration.

**Do green tests guarantee acceptance?** Green deterministic checks are necessary when required. Judge evidence and applicable completion invariants are separate gates. See [verification](VERIFICATION.md).

**Is the public release live-qualified?** The prerelease has historical offline qualification and a separately qualified Docker primitive. See [validation](VALIDATION.md) for the evidence boundary.
