# MemoryLab implementation boundaries

- This repository is independent of QQBot. Do not read or copy private Bot data.
- All commands must name a data directory inside this project. Tests use synthetic data under `.local/verification/`; benchmarks use `.local/benchmarks/`.
- Never enumerate, read, hash, index, back up, export, or recursively delete a persona's `users/` directory. Apply managed-file allowlists before traversal; reject symlinks and Windows junctions.
- Keep lifecycle ledgers outside snapshot replacement. Missing ledgers fail closed; never recreate one for an existing memory database.
- Do not write API credentials, message bodies, model drafts, or request payloads to diagnostic logs.
- Use fake providers for automated tests. Remote requests require explicit runtime configuration. Keep credentials in memory.
- Validate lifecycle, isolation, pause, and snapshot invariants before genuine trial conversations. Do not claim benchmarks or real-model quality that have not been run.
- Before continuing acceptance work after context compaction or in a new conversation, read `docs/acceptance-progress.md` and reconcile it with the latest user instructions, code, evidence, and budget ledger. A checkpoint does not authorize resuming a paused goal or starting remote calls by itself.
