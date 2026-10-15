# Changelog

## 0.1.0 — experimental candidate

- Installable `murphy` command and Python distribution with no runtime dependencies.
- Setup from the signed-in GitHub account; only that immutable owner ID can issue commands. Default same-account execution or optional `init --separate-account` with one separate executor across all projects.
- Isolated executor GitHub login, process-scoped HTTPS credentials and commit author/committer metadata, with no human-account fallback. `auth status` and same-executor `auth login` recovery; existing installations/sessions are not rebound.
- Repository discovery, explicit project connection, managed clones or existing checkouts and automatic base/ID detection.
- Multiple repositories share a persistent serial scheduling order and global controls. Tasks/sessions/checkpoints remain isolated per project.
- Account revalidation, global handling of uncertain outcomes, retained cancellation/draining and report-only retry.
- Dirty source checkout guard: pause before task work, report file names once on the triggering issue, and retry the same blocked command only after explicit resume. Saved task changes remain resumable.
- Explicit `waiting_for_user` turn outcomes free the serial queue for other issues while retaining sessions/worktrees; authenticated continuations resume the same session. Structured final results are validated on both start and resume.
- Operational issue reports/questions are delegated to one worker publisher, preserving full PR/commit/QA details without agent labels, signatures or session metadata in comments.
- Review fixes: reject repository Authorization headers in separate-account mode, retain queued tasks during executor-check failures, and fetch/refresh explicit task bases in single-branch checkouts.
- Legacy explicit-JSON commands and scoped session state remain supported without implicit migration.
- Isolated installation/archive/reproducibility checks, behavior coverage and Linux Python 3.11/3.12 CI.

Limitations: experimental Linux/WSL CLI; polling order during outages is discovery order; no project removal/reconfiguration command, service, parallel execution, automatic merge/deploy or published package/tag/release. Live multi-project operation still needs an authorized pilot.
