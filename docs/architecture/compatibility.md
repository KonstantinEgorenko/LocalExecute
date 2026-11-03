# Compatibility boundary

Canonical semantic packages own implementations. `harness.compatibility.import_mapping` is a closed allowlist, and `harness.compatibility.imports` supplies historical module aliases and package facades. It does not search candidate-provided paths. Installed wheel tests cover import order,identity,from-imports,pickle globals and CLI entrypoints.

Selected `harness.integration` historical adapter names remain supported explicit API readers; tests retaining older issue-family names are regression grouping, not duplicate implementations. Public diagnostic `python -m harness.…` entrypoints retain established locations to avoid unrelated CLI breaks. Serialized receipt/error/schema identifiers and existing SWARM environment-variable spellings remain versioned compatibility contracts. They are not presented as user-facing architecture phases.

Literal removed filesystem paths are outside the new contract; see [Migration](../MIGRATION.md). No global .pth,sitecustomize installation or fallback legacy directory is introduced.
