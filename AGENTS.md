# AGENTS

This is a short working guide for any coding agent contributing to Murphy. Keep detailed setup and operating instructions in `README.md`. Explicit instructions from the person assigning the task take precedence over this guide.

## Start here

1. Read the task or issue, including relevant comments and acceptance criteria.
2. Read the relevant part of `README.md`, then inspect the affected code and nearby tests. Search before opening large files; do not preload unrelated documentation or runtime data.
3. Check the current branch and working tree. Preserve unrelated edits, untracked files and persistent state.
4. If documentation and executable behavior disagree, establish which is stale. Planned features are not evidence of current behavior.

## Working principles

- Use repository-relative paths. Do not assume a particular checkout location, developer account or machine setup.
- Keep changes focused on the requested outcome. Prefer simple code and existing patterns over speculative abstractions or new dependencies.
- Murphy currently uses Python's standard library and targets Linux/WSL. Check `README.md` before changing dependencies or platform assumptions.
- Update tests when behavior changes and documentation when commands, configuration or operating contracts change.
- Resolve routine, reversible choices independently. Ask when missing information materially affects scope, permissions, compatibility or data; report blockers and important assumptions clearly.

## Git and issues

- Normally use a task-specific branch from `develop` and target PRs at `develop`. Follow an explicitly established parent feature branch or another requested base when applicable.
- `master` is the release branch. Direct pushes to shared branches require an explicit request; do not merge, close issues, publish releases or deploy without authorization.
- Read existing issue/PR feedback before continuing work. Update the existing task branch/PR when appropriate instead of creating a replacement.
- Use the locally configured Git/GitHub identity intended for the work. Never impersonate a human, silently change accounts or alter global Git/authentication settings.
- Make commits and PRs easy to review: describe the concrete change, relevant verification and remaining limitations. Identify agent-authored issue reports clearly and preserve earlier reports.

## Verification

Run focused checks while developing. The current local behavior suite is:

```bash
python3 tests/test_bridge.py
python3 tests/test_manager.py
```

It uses temporary repositories and a CLI double; it does not need real model calls, GitHub writes or installation credentials. Process tests require Linux.

For packaging changes, also run `python3 tests/check_package.py` in an environment with the `build` frontend installed; see `RELEASE.md`. It builds, inspects and installs both candidate formats with no GitHub or model access.

- Run the relevant checks before reporting completion. For documentation-only edits, review the diff and run `git diff --check`; a full behavior-suite rerun is normally unnecessary.
- If repository hooks or CI checks are configured, respect them and never bypass hooks with `--no-verify`. Do not repeat successful full checks without a concrete reason.
- Real-model pilots or tests that write to external services need authorization for that scope. Never substitute a live account for a test double merely to make a test pass.
- Report what was verified, what failed and what was not run. Do not claim success from configuration or a queued command alone.

## Configuration and execution boundaries

- Keep installation identities, executable paths and credentials out of source code. Committed examples must use fictional values; authentication belongs to the external CLI tools.
- Never commit local installation configs, authentication files, runtime databases, execution logs or session history.
- Preserve authorization checks, source revalidation, deduplication, durable session bindings and serial execution. Unknown outcomes require explicit reconciliation, not automatic replay.
- The executor can edit, test, commit and push within its assigned task. Preserve the permission profile and task scope; broader filesystem access, merge, deployment or releases require explicit authorization and suitable verification.
- Keep work inside this repository unless the task explicitly requires another project. Do not reset, clean, delete or mutate another project's checkout or runtime as an incidental part of this work.
