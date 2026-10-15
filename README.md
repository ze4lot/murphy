# Murphy

Murphy lets you work on your GitHub projects by commenting on issues. Install it on Linux/WSL, sign in to GitHub and Codex, connect your repositories and run one local worker. It accepts commands **only from the GitHub account you connected**. The same account posts reports and opens PRs; a separate bot account is unnecessary.

Multiple projects share one durable queue. Murphy runs **one task at a time across all connected repositories**, gives each issue its own branch/worktree and keeps its Codex session for later continuations. It can edit, test, commit, push a task branch and create/update PRs when your instructions call for them. Merge, deployment and releases require separate authorization.

Version `0.1.0` is experimental. Keep backups and start with a disposable project. Nothing is installed as a service.

## Install

Requirements:

- Linux or WSL2 with Python 3.11+, `/proc` and pidfd support. Local validation uses Python 3.12; CI covers 3.11 and 3.12. Native Windows/macOS are not supported.
- Git, [GitHub CLI](https://cli.github.com/) and [Codex CLI](https://developers.openai.com/codex/cli/) installed and on your PATH.
- Your GitHub account has read/write access to the projects you connect, including issue comments, contents and PRs. Its token must also permit those operations. Git fetch/push authentication and author identity must be configured for the same intended account.
- An authenticated Codex installation with a model supporting the requested reasoning setting. The executor permission profile was exercised with Codex CLI 0.162.1; other versions are not qualified.

Install from a local checkout of the candidate branch/commit (PyPI publication is a separate step):

```bash
python3 -m venv ~/.local/share/murphy-venv
~/.local/share/murphy-venv/bin/python -m pip install /path/to/murphy
~/.local/share/murphy-venv/bin/murphy --help
```

Add that environment's `bin` directory to PATH or use the full command path. A supplied `.whl` candidate file can be installed with the same `pip install` command. The distribution name is `murphy-github`; the command is `murphy`. There are no Python runtime dependencies.

Authenticate with your own accounts before setup:

```bash
gh auth login
codex login
```

For HTTPS Git access, `gh auth setup-git` can configure Git to use GitHub CLI credentials. For SSH, configure the key for the intended GitHub account. Murphy does not switch your Git identity or copy credentials.

## First run and multiple projects

```bash
murphy init
murphy projects available
murphy projects add example/api
murphy projects add example/website
murphy projects list
murphy run
```

`init` reads the currently authenticated **GitHub CLI account**, not your Linux username. Its immutable GitHub user ID becomes the only permitted command author. No manual login or numeric ID fields are needed. The account also publishes reports. `init` detects missing tools and does not replace an existing installation identity.

`projects available` lists accessible, active repositories and whether GitHub reports push permission. Only explicitly added repositories are watched. Adding a project obtains its canonical name, numeric ID and default branch from GitHub, checks access and clones it locally through `gh`. It never enables every repository automatically.

To use your existing checkout or another task base:

```bash
murphy projects add example/api --checkout /path/to/api --base develop
```

The source checkout is not switched, cleaned, reset or copied. Task worktrees live outside it. A managed clone failure preserves partial files; inspect them and explicitly use `--checkout` after fixing the problem. Connecting a repository twice fails without moving its activation boundary. An empty repository needs an initial commit before connection. A non-default task base must already exist on the remote.

Choose external tool wrappers once, if needed:

```bash
murphy init --gh /opt/example/bin/gh-wrapper --git /opt/example/bin/git-wrapper --codex codex
```

These must be executable names or paths, not shell command strings. Settings remain private installation state. The ordinary setup requires no JSON editing. Optional per-project `--base` and `--model` settings are supplied to `projects add`; otherwise GitHub's default branch and Codex's default model are used. There is no project removal/reconfiguration command in this candidate; inspect saved work before creating a separate installation.

The account is revalidated during polling, before source validation and before GitHub reads/writes. A changed account stops the worker; active descendants are drained and an uncertain turn requires reconciliation. Restore the original account before resuming. For another account, initialize a separate `--state-dir`; existing tasks are never silently rebound to it. The global execution lock still prevents overlapping Murphy workers.

## Give Murphy a task

From the account used for `init`, add a **new issue comment**:

```text
/murphy start
Implement this issue, run the relevant tests and open a draft PR against develop.
```

An inspection-only example:

```text
/murphy start
Read the issue and relevant code. Report findings only; do not edit files, run tests, commit or push.
```

Continue an existing task:

```text
/murphy continue
Address the feedback above and run the relevant tests.
```

The command occupies its own first line. Instructions follow on later lines. LF/CRLF and spaces around the command are accepted; quoted commands, inline instructions and indented code blocks are rejected. Commands from other accounts, edited comments and comments predating project activation are ignored. Reports are plain results, not `/murphy` commands, so posting with your own account does not create a command loop.

A new issue gets `codex/issue-<number>-murphy`, its own worktree and a persistent session. Identical issue numbers in different projects have separate state, paths and sessions. A direct parent with one unambiguous issue-specific open PR can supply a feature base; otherwise the project's base is used. Existing issue branches/worktree paths are never overwritten or silently adopted. Automatic start targets open issues, not PRs.

A continuation resumes the saved UUID and accepts unchanged uncommitted work left by the previous turn. Murphy saves branch/Git directories, HEAD and a fingerprint of index/tracked/untracked changes after each execution. Changes made between turns outside Murphy pause the queue without overwriting them. This detects between-turn changes, not the author of simultaneous human edits. Repeated start fails without replacing a session; continuation without a session fails without inventing one.

### Reasoning and existing task registration

New sessions default to **high**. Override with exactly one plain line in the issue body, outside code fences/quotes:

```text
Reasoning: medium
```

Accepted values: `low`, `medium`, `high`, `xhigh`, `max`, `ultra`; your selected model must support the value. Invalid/duplicate metadata fails before worktree creation or a model call. The effort is saved for continuations; later issue edits do not change it. Existing legacy sessions retain their settings.

For an existing clean task checkout, register issue/PR aliases explicitly:

```bash
murphy register existing-task --project example/api \
  --cwd /path/to/task-worktree --branch codex/existing-task \
  --item 123 --item 124 --reasoning high
```

The first `start` creates its session and later `continue` comments on either alias resume it. Registration does not infer a session or attach an already-running automatic task to a PR. Until an alias is registered, continue automatic tasks on their original issue.

Trusted local operators can use `murphy enqueue TASK start|continue --project OWNER/REPO --prompt TEXT`. `murphy ingest-comment ID --project OWNER/REPO` refetches one comment through the same activation, author and deduplication checks. Both use the shared queue.

## Queue, stop and recovery

Defaults: poll every 30 seconds, execution timeout 600 seconds. While idle Murphy calls GitHub, not a model. Change them with `murphy run --poll-seconds 5 --timeout 180`. `murphy run --once` performs one scheduling pass and runs at most one task.

Commands receive installation-wide IDs, shown in reports and `murphy status`. Newly discovered commands are enrolled by comment creation time/ID; already-enrolled commands keep their durable order. A repository temporarily unavailable to polling retains its cursor and joins the queue on recovery. This is discovery order, not a promise of perfect GitHub-wide chronology during outages.

Controls work locally or as whole comments in connected repositories, including during another project's active turn:

| Local command | GitHub comment | Effect |
| --- | --- | --- |
| `murphy pause` | `/murphy pause` | Let the current turn finish; hold all projects. |
| `murphy resume` | `/murphy resume` | Resume after all unknown outcomes are resolved. |
| `murphy cancel 12` | `/murphy cancel 12` | Cancel the global command ID, even in another project. |
| `murphy status` | — | Inspect projects, commands and pause state. |

Stop the foreground worker with Ctrl+C or SIGTERM. It cancels/drains its descendants before releasing the slot. Partial edits and commits remain available. One inherited lock at `~/.local/state/murphy/worker.lock` covers all Murphy workers and descendants, including legacy workers and other `--state-dir` installations. Direct Codex runs outside Murphy are not governed by this lock.

An unclean exit, timeout, changed checkout/session or uncertain execution marks the command `unknown` and holds **all projects**. Never replay it automatically. With the worker stopped, inspect its saved execution JSONL/stderr, exact session and surviving processes, then explicitly resolve:

```bash
murphy resolve 12 --outcome failed --note 'Inspected logs and confirmed all descendants exited.'
murphy resume
```

Resolution changes bookkeeping only. A branch/worktree creation plan is persisted before creation. Uncertain creation preserves the plan and files. `run --once` returns 2 if unknown outcomes remain. Publication failures retry only reports, with an execution marker reconciling an uncertain POST; they never repeat model work.

## State, configuration and execution scope

Default installation directory: `~/.local/state/murphy/manager`. Supply `--state-dir PATH` **before** the subcommand to use another directory. Keep state private and outside Git:

| Location relative to installation | Contents |
| --- | --- |
| `manager.sqlite` | Connected account/tool settings, projects, durable global command IDs/order and pause flag. |
| `checkouts/<repository-id>/` | Managed source clones. |
| `projects/<repository-id>/state.sqlite` | Project authorization/activation, task/session bindings and execution outcomes. |
| `projects/<repository-id>/worktrees/` | Automatic task checkouts. |
| `projects/<repository-id>/*.jsonl`, `*.stderr` | Execution logs; may contain issue text and model output. |

Do not edit SQLite files while Murphy runs. Do not delete state to recover an uncertain turn. Credentials stay in Git/GitHub/Codex tools. Account/tool paths in these databases are local settings, never release contents.

Codex gets a named permission profile extending `:workspace`: the assigned task worktree and its Git metadata are writable, network is enabled, interactive approvals are disabled. Shared Git refs/objects need explicit access for linked worktrees; sibling source working files are not granted. Both start/resume receive the same permission settings; there is no full-access fallback. The child uses the existing Codex login without copying auth files, removes inherited OpenAI API-key variables and ignores unrelated Codex user configuration. Task instructions and target `AGENTS.md` still define authorized scope.

### Prototype compatibility

The original explicit-JSON, single-project interface remains available via `python3 bridge.py --config PATH ...` from source, or `murphy --config PATH ...` after installation. `config.example.json` contains fictional values only; it is shipped in source and installed under the environment's `share/murphy/`. Fields: `repository`, `repository_id`, `bot_user_id` (publishing account), `owner_user_id`, `owner_login`, and optional `git`, `gh`, `codex` executables. Publishing and owner IDs may now match. Separate legacy bot accounts remain supported.

Existing scoped prototype databases and saved sessions are not migrated into the new manager or modified during installation. Continue them with their original config/state paths. Unscoped older pilot state is rejected. New manager projects use new activation boundaries; do not run both interfaces against the same tasks.

## Development and controlled pilot

```bash
python3 tests/test_bridge.py
python3 tests/test_manager.py
python3 -m venv .venv
.venv/bin/python -m pip install 'build==1.2.2.post1'
.venv/bin/python tests/check_package.py
```

Behavior tests use temporary Git repositories and a Codex CLI double. They do not call real models, publish to GitHub or require private configuration. The package check builds both distribution formats twice, compares hashes, inspects archive contents and installs each in a fresh environment with an empty HOME. Build tools may be downloaded from the Python package index; GitHub/Codex credentials are unnecessary. GitHub CI runs only after a PR from this repository's `develop` branch is merged into `master`, testing that merge's exact commit. Task PRs and ordinary pushes do not run CI; there is no manual trigger. See [RELEASE.md](RELEASE.md) for exact build/verification commands and promotion steps.

Optional native sandbox check (no model call or GitHub write):

```bash
python3 tests/check_sandbox.py --codex codex
```

A controlled live pilot requires explicit authorization: use a disposable repository with your own credentials, a separate state directory and the intended base. Start with the inspection-only example; verify the report and session, then authorize a small edit/test/commit/PR task and a continuation. Confirm one UUID, isolated worktrees, global serial order across two projects and stop/recovery behavior. Do not publish, merge or deploy as part of this pilot. This release preparation does not claim a new live multi-project pilot was performed.

Automatic merge/deployment, a broker, dashboard, parallel execution, native Windows support and service packaging are outside this candidate.

## License

MIT. See [LICENSE](LICENSE).
