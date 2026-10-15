# Changelog

## 0.1.0 — experimental candidate

- Installable `murphy` command and Python distribution with no runtime dependencies.
- Setup from the signed-in GitHub account; only that immutable user ID can issue commands. Reports use the same account.
- Repository discovery, explicit project connection, managed clones or existing checkouts and automatic base/ID detection.
- Multiple repositories share a persistent serial scheduling order and global controls. Tasks/sessions/checkpoints remain isolated per project.
- Account revalidation, global handling of uncertain outcomes, retained cancellation/draining and report-only retry.
- Legacy explicit-JSON commands and scoped session state remain supported without implicit migration.
- Isolated installation/archive/reproducibility checks, behavior coverage and Linux Python 3.11/3.12 CI.

Limitations: experimental Linux/WSL CLI; polling order during outages is discovery order; no project removal/reconfiguration command, service, parallel execution, automatic merge/deploy or published package/tag/release. Live multi-project operation still needs an authorized pilot.
