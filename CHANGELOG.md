# Changelog

## Unreleased

- Run all issue branches and persistent sessions in the configured local project checkout, using the existing development Compose services and database.
- Require `projects add --checkout PATH`; stop creating managed clones, task worktrees and per-session environments.
- Switch back to saved task branches only from a clean checkout with an unchanged task commit; preserve resumable task edits and report conflicting local changes.
- Remove automatic worktree and Docker/Podman resource cleanup. Preserve legacy worktrees and sessions for explicit migration; old paths never silently execute on continuation.

## 0.1.0 — 2026-10-18 (experimental)

- Installable `murphy` command and Python distribution with no runtime dependencies.
- Setup from the signed-in GitHub account; only that immutable owner ID can issue commands. Default same-account execution or optional `init --separate-account` with one separate executor across all projects.
- Isolated executor GitHub login, process-scoped HTTPS credentials and commit author/committer metadata, with no human-account fallback. `auth status` and same-executor `auth login` recovery; existing installations/sessions are not rebound.
- Repository discovery, explicit project connection, managed clones or existing checkouts and automatic base/ID detection.
- Multiple repositories share a persistent queue and global controls; execution remains serial within each project. Tasks/sessions/checkpoints remain isolated per project.
- Bounded parallel execution across projects: two by default, configurable with `run --max-parallel-projects` (1 for fully serial work). Separate executor processes preserve configuration/credential isolation, per-project order, targeted cancellation, descendant draining and conservative restart/unknown-outcome handling.
- Per-project polling intervals via `projects add -i/--interval`, defaulting to 60 seconds for new and existing projects. Saved intervals appear in project listings/status; explicit `run --poll-seconds` remains a temporary override for all projects.
- Account revalidation, global handling of uncertain outcomes, retained cancellation/draining and report-only retry.
- Dirty source checkout guard: pause before task work, report file names once on the triggering issue, and retry the same blocked command only after explicit resume. Saved task changes remain resumable.
- Explicit `waiting_for_user` turn outcomes free the serial queue for other issues while retaining sessions/worktrees; authenticated continuations resume the same session. Structured final results are validated on both start and resume.
- Operational issue reports/questions are delegated to one worker publisher, preserving full PR/commit/QA details without agent labels, signatures or session metadata in comments.
- Review fixes: reject repository Authorization headers in separate-account mode, retain queued tasks during executor-check failures, and fetch/refresh explicit task bases in single-branch checkouts.
- Automatic OpenSSH ControlMaster sockets in a private project directory writable by the Codex sandbox, with scoped client configuration, finite idle expiry and cleanup after task descendants exit. No writable access to SSH keys or manual master preparation/closure is required.
- Legacy explicit-JSON commands and scoped session state remain supported without implicit migration.
- Isolated installation/archive/reproducibility checks, behavior coverage and Linux Python 3.11/3.12 CI.

Limitations: experimental Linux/WSL CLI; polling order during outages is discovery order; no project removal/reconfiguration command, service, parallel execution within one project or automatic merge/deploy. Live multi-project operation still needs an authorized pilot. SSH connection reuse is limited to one sandbox command's lifetime. Event-driven delivery remains tracked separately in issue #4.
