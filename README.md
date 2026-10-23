# Murphy

<p align="center">
  <img src="assets/murphy_with_screen.png" alt="Murphy, a robotic goat with a holographic screen" width="420">
</p>

Murphy lets you work on your GitHub projects by commenting on issues. Install it on Linux/WSL, sign in to GitHub and Codex, connect your repositories and run one local worker. It accepts commands **only from your connected owner account**. By default that account also does the work; optionally sign in a separate executor account for reports, commits, pushes and PRs.

Multiple projects share one durable queue. Murphy runs **one task at a time per repository**, with up to **two projects executing concurrently** by default, gives each issue its own branch/worktree and keeps its Codex session for later continuations. It can edit, test, commit, push a task branch and create/update PRs when your instructions call for them. Merge, deployment and releases require separate authorization.

Version `0.1.0` is experimental. Keep backups and start with a disposable project. Nothing is installed as a service.

## Install

Requirements:

- Linux or WSL2 with Python 3.11+, `/proc` and pidfd support. Local validation uses Python 3.12; CI covers 3.11 and 3.12. Native Windows/macOS are not supported.
- Git, [GitHub CLI](https://cli.github.com/) and [Codex CLI](https://developers.openai.com/codex/cli/) installed and on your PATH.
- The executor's GitHub account (your own by default) has read/write access to connected projects, including issue comments, contents and PRs. Its token must also permit those operations. In single-account mode, configure Git fetch/push authentication and author identity for that account. Separate-account mode configures process-scoped HTTPS credentials and commit identity for its executor automatically.
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

For single-account HTTPS Git access, `gh auth setup-git` can configure Git to use GitHub CLI credentials. For single-account SSH, configure the intended account's key. Separate-account mode needs neither command nor a separate SSH key; it leaves your normal Git/GitHub settings unchanged.

## First run and multiple projects

```bash
murphy init
murphy projects available
murphy projects add example/api
murphy projects add example/website
murphy projects list
murphy run
```

`init` reads the currently authenticated **GitHub CLI account**, not your Linux username. Its immutable GitHub user ID becomes the only permitted command author. No manual login or numeric ID fields are needed. Without `--separate-account`, the same account does the work and publishes reports. `init` detects missing tools and does not replace an existing installation identity.

### Optional separate executor account

Instead of plain `init`, choose the separate-account mode when creating an installation:

```bash
murphy init --separate-account
murphy auth status
murphy projects available
murphy projects add example/api
murphy run
```

Murphy first identifies the owner from your normal `gh` login, then opens GitHub CLI's browser login for a **different executor account** in a private configuration. Choose that account in the browser; if your browser is already signed in to your personal account, use the appropriate browser profile/account. Setup displays both detected logins and refuses to save the same account twice. One owner and one executor serve all connected projects; each project still has its own worktrees/sessions and all projects share the queue.

The owner writes `/murphy` comments. The executor reads them, accepts only the saved owner's immutable ID, and publishes reports/PRs. Repository discovery and access checks use the executor's account. Give that account access to each desired repository through GitHub before connecting it. Comments from the executor or other people do not authorize work.

After setup the worker does not need the owner's login or token. Switching/logging out of your ordinary `gh` does not change the owner or affect the separate executor. Murphy supplies the private executor configuration to its own commands and Codex descendants, removes inherited GitHub tokens/credential overrides, and verifies the executor ID before GitHub operations and Git credential retrieval. GitHub operations use HTTPS, including process-only rewriting of recognized GitHub SSH URLs; the source remote URL stays unchanged. Git author/committer metadata uses the executor's public display name (or login) and `ID+LOGIN@users.noreply.github.com`. Older accounts may need to enable the ID-based noreply address in [GitHub email settings](https://docs.github.com/en/account-and-profile/reference/email-addresses-reference) for attribution.

Git credentials come from the executor's `gh` login through a scoped helper. Human credential helpers and system/global Git settings are not loaded for these separate-account processes; repository settings/hooks remain in place. No global Git config or default `gh` login is edited. Use standard `gh`/`git` executables in this mode; custom wrappers must honor the supplied executor environment. A wrapper that forces another account is incompatible.

Separate-account mode refuses checkouts with repository `http.*.extraHeader` Authorization headers, including URL-specific headers, because these can bypass the executor's credential helper. Inspect and remove those local settings yourself before connecting or retrying the checkout; Murphy does not rewrite them or print their values.

`murphy auth status` shows the saved owner, executor, mode and current executor authentication (exit 1 on missing/changed authentication). To restore an expired/logged-out executor, stop the worker and run:

```bash
murphy auth login
murphy auth status
```

This reauthenticates the **same saved executor**; it cannot rebind the installation to another account. A changed executor stops work and any uncertain active turn needs the normal reconciliation procedure. There is no fallback to the owner's credentials. To select another owner/executor or switch an existing installation's mode, create a new `--state-dir`; existing tasks, queues and sessions are preserved rather than migrated. Single-account installations restore authentication directly with `gh auth login`.

### Connecting projects

`projects available` lists accessible, active repositories and whether GitHub reports push permission. Only explicitly added repositories are watched. Adding a project obtains its canonical name, numeric ID and default branch from GitHub, checks access and clones it locally through `gh`. It never enables every repository automatically.

To use your existing checkout or another task base:

```bash
murphy projects add example/api --checkout /path/to/api --base develop
```

Each connected repository has its own polling interval, saved in seconds. The default is **60 seconds**; set a different whole number (minimum 1) when adding it:

```bash
murphy projects add example/website -i 120
murphy projects add example/tools --interval 30
murphy projects list
```

`projects list` and `status` show each project's saved `interval`. Existing connected projects receive the 60-second default on upgrade without resetting activation cursors, commands or sessions. Different intervals affect when comments are discovered. Projects share one durable queue, with serial execution within each repository and a bounded number of projects running concurrently.

The source checkout is not switched, cleaned, reset or copied. Task worktrees live outside it. A managed clone failure preserves partial files; inspect them and explicitly use `--checkout` after fixing the problem. Connecting a repository twice fails without moving its activation boundary. An empty repository needs an initial commit before connection. A non-default task base must already exist on the remote.

New automatic worktrees and executor processes use a `022` umask so application containers can read source files (normally `644`) and traverse workspace directories (normally `755`). Installation state and execution logs retain the worker's private `077` umask. Existing workspaces are not recursively chmodded; repository-specific credentials and secrets must keep their own restrictive permissions. Container networks, ports and test data isolation belong to the target repository's QA workflow, not Murphy.

Choose external tool wrappers once, if needed:

```bash
murphy init --gh /opt/example/bin/gh-wrapper --git /opt/example/bin/git-wrapper --codex codex
```

These must be executable names or paths, not shell command strings. Settings remain private installation state. The ordinary setup requires no JSON editing. Optional per-project `--base` and `--model` settings are supplied to `projects add`; otherwise GitHub's default branch and Codex's default model are used. There is no project removal/reconfiguration command in this candidate; inspect saved work before creating a separate installation.

The executor account is revalidated during polling, before source validation and before GitHub reads/writes. In single-account mode this is your ordinary `gh` account; in separate-account mode it is the private executor login. A changed executor stops the worker; active descendants are drained and an uncertain turn requires reconciliation. Restore the original executor before resuming. For another identity pair, initialize a separate `--state-dir`; existing tasks are never silently rebound to it. The global coordinator lock still prevents overlapping Murphy installations/workers; it does not serialize project executors inside the same coordinator.

## Give Murphy a task

From the owner account identified at the beginning of `init`, add a **new issue comment**:

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

Before invoking Codex for an accepted `start` or `continue`, Murphy checks the triggering issue/PR's existing GitHub Projects memberships and sets the `Status` field to `In Progress` wherever that option exists (case-insensitive). Items already in progress need no write. Archived items, closed projects and projects without that status option are skipped; Murphy never adds items or creates fields/options. The check runs when the queued command begins execution, after source/session/checkout validation. Local-only execution skips it. The executor's token needs Projects read/write access (`project` scope for a classic token), plus access to those projects. A lookup or update failure stops the command before any model invocation and appears in its report; correct access and submit the appropriate `start` or `continue` again. Earlier successful status updates may remain if a later project fails. Murphy does not change status at turn completion.

When the executor needs an owner decision, it ends its turn with `waiting_for_user` and a concrete question. The saved session, branch/worktree and uncommitted checkpoint stay available; its execution slot is released for another queued command. Other projects can keep running, and each project still has only one active execution. Reply in that issue with `/murphy continue` on the first line and your answer below it. An ordinary comment does not resume work. The continuation joins the existing queue and does not interrupt an active turn. Restarting the worker or running `murphy resume` does not answer the question or replay the waiting turn.

Both new and resumed Codex turns use `--output-schema` with explicit `outcome` (`succeeded` or `waiting_for_user`) and human-readable `message`. Murphy validates the final JSON rather than inferring intent from punctuation. `succeeded` means the requested turn completed, not that the issue was closed or its PR merged. Missing/malformed results become `unknown` and hold the queue for reconciliation. Existing saved sessions use the same result contract on their next continuation.

The worker publishes the turn's report or question once. The executor is instructed to return the complete report in `message`, including repository-required PR, commit and QA details, and to delegate operational issue comments to Murphy even when a repository workflow normally posts them directly. Repository rules do not need editing. Reports omit agent prefixes/signatures and internal session/turn/execution metadata; GitHub already identifies the author. Standard outer `[Codex][DONE]`-style labels and trailing Codex/Murphy signatures are removed without changing report content or examples. Reports over 12000 characters are shortened with a notice; full text and technical details remain in `murphy status` and private logs. Other owner-requested issue edits and PR operations remain available.

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

Defaults: use each project's saved interval (60 seconds unless specified at connection), execution timeout 600 seconds. While idle Murphy calls GitHub, not a model. Every project is first polled on worker startup, then independently at its interval, including while another project executes. Controls posted on GitHub are discovered at the interval of the repository where they were posted; local controls do not wait for a GitHub poll.

The existing `murphy run --poll-seconds 5 --timeout 180` explicitly overrides **all** project intervals for that run. Omitting `--poll-seconds` uses the saved intervals; the override does not change them, and `projects list`/`status` still display the saved values. Remove this flag from old launch commands to use per-project intervals. `murphy run --once` performs one synchronous scheduling pass, runs at most one task and waits for it to finish; it does not fill parallel slots.

`murphy run` defaults to at most **two simultaneously executing projects**, with **one active command per project**. Change the bound for that run:

```bash
murphy run --max-parallel-projects 2
murphy run --max-parallel-projects 1  # Fully serial operation
```

The coordinator polls projects, processes global controls and publishes reports. Each active project runs in its own executor process with fresh database connections and isolated repository configuration. Dispatch selects the oldest eligible queued command, skipping projects that are already active; order within a project is preserved. A queued command in a busy project does not prevent a different project from using a free slot.

Worktrees isolate Git working files, not host resources. Projects can share Docker services, databases, ports, CPU/RAM and account limits. Use a limit of `1` when their tests or workflows require exclusive shared infrastructure. Direct Codex runs outside Murphy are not covered by its scheduling.

Commands receive installation-wide IDs, shown in `murphy status`. Newly discovered commands are enrolled by comment creation time/ID; already-enrolled commands keep their durable order. A repository temporarily unavailable to polling retains its cursor and joins the queue on recovery. This is discovery order, not a promise of perfect GitHub-wide chronology during outages.

If the periodic executor check fails because authentication or the API is unavailable, queued tasks stay queued. Scheduling resumes after a successful check; the shortest effective project interval controls these periodic checks and retries. Checks immediately before GitHub operations remain in place.

Before a task starts or continues, Murphy checks the connected source checkout for staged, unstaged and untracked files (ignored files do not block it). Uncommitted changes mark the command `blocked` and pause the shared queue before any fetch, new task worktree or model call. The triggering issue receives a report with relative file names, never file contents; long lists are shortened with a notice. Publication retries reconcile the same comment and do not repeatedly notify while paused. The pause and command survive restarts.

Clean up the source checkout yourself, then run `murphy resume` or post `/murphy resume` as the owner in a connected project. Cleanup alone does not restart work. Resuming retries the same command after author/source revalidation and another checkout check; a still-dirty source blocks again. `murphy cancel ID` also accepts blocked commands. Murphy never commits, stashes, resets or deletes the owner's changes to clear this guard. An assigned task's own saved uncommitted checkpoint remains valid for continuation, including when an explicitly registered task uses the source checkout itself. Other changes to its task worktree retain the existing `unknown`/reconciliation protection.

Controls work locally or as whole comments in connected repositories, including during another project's active turn:

| Local command | GitHub comment | Effect |
| --- | --- | --- |
| `murphy pause` | `/murphy pause` | Let active turns finish; hold new dispatches in all projects. |
| `murphy resume` | `/murphy resume` | Retry blocked commands after cleanup; resume after all unknown outcomes are resolved. |
| `murphy cancel 12` | `/murphy cancel 12` | Cancel the global command ID, even in another project. |
| `murphy status` | — | Inspect projects, commands and pause state. |

### Terminal monitor

```bash
murphy monitor          # Watch a worker that is already running
murphy monitor --once   # Print one snapshot and exit
murphy run --monitor    # Start the worker with a live table
murphy run -m           # Short alias for the same mode
```

The table shows installation-wide command IDs, repositories, issue numbers and saved/cached titles,
status and execution time. It sizes its columns to the terminal and their content, keeps visible
text when shortening a cell, and uses a stacked layout in narrow terminals. The footer stays
visible when the task list is taller than the terminal; a counter shows omitted rows.

Only the latest command per task/session is shown, so an earlier question disappears after a
continuation completes. Queued, running, blocked and unknown commands always remain visible,
even if a later turn exists. Active and unresolved commands appear first, followed by up to five
recent completions. Local commands show their task name. A queued issue's title may be unavailable
until the worker fetches it. Pause/resume/cancel control commands are omitted.
The header distinguishes an idle, busy, paused or stopped worker and shows active/queued counts,
followed by `zakończone`: the number of closed/merged/deleted items hidden from the table.
An old `running` row without its coordinator is displayed as `BRAK WORKERA`, not as live work;
the monitor does not reconcile it or change the recorded outcome.

While a command runs, the view shows the latest command or agent message found in a bounded
tail of its existing execution JSONL. `Log: N s temu` measures the last log write, not the
duration of the displayed action. Silence is not treated as a hang. Waiting, blocked, failed
and unknown commands show their saved explanation. Long text is shortened to fit the terminal;
narrow terminals use two lines per task. The monitor makes no model calls or GitHub requests.

The last line shows Codex account quota **usage** percentages for the available windows
(for example `Codex: 5h 25% · 7d 81% zużycia`), followed by the keyboard shortcut. In narrow
terminals the shortcut moves above the quota line so it stays visible. The monitor
uses the installation's saved Codex executable and its existing login to read
[`account/rateLimits/read`](https://learn.chatgpt.com/docs/app-server#auth-endpoints).
This account-wide usage includes work outside Murphy. A background read runs at most once
per minute without holding up task rendering; `monitor --once` waits up to five seconds for
its initial read. Missing login, unavailable service or unsupported CLI shows `Codex: brak danych`
instead of a percentage. This helper creates no model turns and never resets any quota.

The worker refreshes a local issue/PR status cache with at most one GraphQL batch (up to 50
distinct inactive items) per repository every five minutes. Recently checked items are skipped
across worker restarts; closed/merged items are rechecked at most hourly to detect reopening.
Normal executor identity verification still applies to each request. Only sessions represented
in local task state are queried, not the repository's entire issue history. Failed requests
back off for five minutes and preserve the last known states. A missing/inaccessible item does
not mean it is closed and does not discard valid results for the other items in the batch.
Only numbers missing from GraphQL receive an additional REST lookup. An explicit HTTP 410
is cached as `DELETED` and is not polled again. HTTP 404, missing permissions and failed
lookups remain uncertain; they never imply deletion. Valid results for other items survive
a failed REST fallback. Unverified `UNKNOWN` cache entries are retried once on worker startup,
then follow the same five-minute batch interval, so old unresolved entries can be reconciled
immediately after upgrading.

Closed issues, closed/merged PRs and confirmed deleted items disappear from the default view,
with their count shown as `zakończone` in the header.
This is presentation only: commands, reports, worktrees and sessions are retained, and
`murphy status` still includes the full command history. Queued/running commands and blocked or
unknown outcomes are never hidden or automatically resolved by this cache. Closure is based
on the tracked issue/PR itself; merging a separate PR does not imply its issue is closed.
An open issue can take up to the next refresh to disappear after closing; reopening a cached
closed item is detected by its slower refresh. The worker updates this cache even without `-m`,
so standalone monitors share it without multiplying GitHub requests.

The display normally refreshes once per second, independently of worker API calls. `Ctrl+C`
in `murphy monitor` only closes the view; in `murphy run -m` it stops the worker and waits for
the usual executor/descendant cleanup. The latter runs the regular worker as a child process,
preserves its options and exit status, and shows worker diagnostics below the table. Full
diagnostics are printed to stderr when the worker exits. The terminal screen/cursor are restored
on exit. When stdout is redirected (or `TERM=dumb`), `monitor` prints once; `run -m` prints
plain snapshots when the state changes, without terminal escape sequences. `murphy status`
keeps its existing JSON output.

The standalone monitor opens existing SQLite state read-only and never creates an installation,
enrolls commands or acquires the worker lock. Workers started with this version also maintain
`worker.json` under that installation while holding their usual lock. The monitor validates its
PID and Linux process birth time, so stale metadata or a reused PID cannot claim a live worker.
Restart an older worker with this version to make its identity visible. The plain snapshot data
is separate from terminal rendering for a later HTTP integration; no HTTP server is included.

Stop the foreground worker with Ctrl+C or SIGTERM. It stops all active project executors and drains their descendants before releasing the coordinator lock. Partial edits and commits remain available. One inherited lock at `~/.local/state/murphy/worker.lock` covers all Murphy workers and descendants, including legacy workers and other `--state-dir` installations. Direct Codex runs outside Murphy are not governed by this lock.

A dispatch reservation is persisted before starting an executor. An unclean executor exit, timeout, changed checkout/session or uncertain execution marks the command `unknown` and blocks **new dispatches across all projects**; other already active turns may finish. A dirty source checkout has the same global scheduling hold. Cancellation/timeout drains only the affected execution, not another active project. Coordinator failure drains all active executions; uncertain results require reconciliation. If the coordinator is killed, surviving executors detect its loss, drain their descendants and record unknown outcomes. Interrupted reservations are reconciled as unknown on restart, never automatically replayed.

With the worker stopped, inspect the affected command's saved execution JSONL/stderr, exact session and surviving processes, then explicitly resolve:

```bash
murphy resolve 12 --outcome failed --note 'Inspected logs and confirmed all descendants exited.'
murphy resume
```

Resolution changes bookkeeping only. A branch/worktree creation plan is persisted before creation. Uncertain creation preserves the plan and files. `run --once` returns 2 if unknown outcomes remain. Publication failures retry only reports, with an execution marker reconciling an uncertain POST; they never repeat model work.

## State, configuration and execution scope

Default installation directory: `~/.local/state/murphy/manager`. Supply `--state-dir PATH` **before** the subcommand to use another directory. Keep state private and outside Git:

| Location relative to installation | Contents |
| --- | --- |
| `manager.sqlite` | Saved owner/executor IDs and non-secret tool settings, projects, durable global command IDs/order and pause flag. |
| `worker.json` | Current coordinator PID, process birth time and concurrency limit; removed on normal exit and validated against `/proc` by the monitor. |
| `auth/executor/` | Separate-account GitHub CLI configuration, when enabled. gh manages credentials in the system credential store; if unavailable it can store its token here. Keep this directory private. |
| `checkouts/<repository-id>/` | Managed source clones. |
| `projects/<repository-id>/state.sqlite` | Project authorization/activation, task/session bindings and execution outcomes. |
| `projects/<repository-id>/worktrees/` | Automatic task checkouts. |
| `projects/<repository-id>/*.jsonl`, `*.stderr` | Execution logs; may contain issue text and model output. |

Do not edit SQLite files while Murphy runs. Do not delete state to recover an uncertain turn. Credentials stay under GitHub/Codex CLI management; Murphy does not store tokens in its databases. GitHub CLI's credential-store/file fallback is described in its [login documentation](https://cli.github.com/manual/gh_auth_login). Treat the entire installation directory, including `auth/executor`, as private and keep it outside Git. Account/tool paths in databases are local settings, never release contents.

Codex gets a named permission profile extending `:workspace`: the assigned task worktree and its Git metadata are writable, network is enabled, interactive approvals are disabled. Shared Git refs/objects need explicit access for linked worktrees; sibling source working files are not granted. Both start/resume receive the same permission settings; there is no full-access fallback. The child uses the existing Codex login without copying auth files, removes inherited OpenAI API-key variables and ignores unrelated Codex user configuration. Task instructions and target `AGENTS.md` still define authorized scope.

### OpenSSH connection sharing in the sandbox

Murphy automatically creates a private `0700` socket directory under `~/.cache/murphy/<project-hash>` and adds **only that directory** to the Codex profile's writable paths on both start and resume. `MURPHY_SSH_CONTROL_DIR` is set explicitly in the Codex shell environment. The hash identifies the source Git directory: linked task worktrees share a project directory; separate repositories/installations have different paths. The Linux home and ownership checks separate local users. Existing symlinks, foreign ownership or permissions other than `0700` in Murphy's socket directories stop execution; Murphy never fixes their permissions silently. No write access to `~/.ssh`, private keys or other projects' socket directories is added. These are write boundaries, not a new read/credential isolation mechanism; existing Codex read access and authentication remain as before.

One-time operator setup, after installing this version: add the following to the **client** SSH configuration used by the authorized destination. Replace `deploy.example.invalid` with the exact destination or alias used by SSH/SCP. Put the block **before** existing `Host` defaults for `ControlPath`, `ControlMaster` or `ControlPersist` (OpenSSH uses the first matching value). Keep existing identity settings and trusted host keys.

```sshconfig
Match originalhost deploy.example.invalid exec "test x${MURPHY_SSH_CONTROL_DIR:+set} = xset"
    ControlMaster auto
    ControlPersist 60
    ControlPath ${MURPHY_SSH_CONTROL_DIR}/%C
Match all
```

The `Match` condition leaves ordinary SSH outside Murphy using its existing configuration. `%C` separates destination host, port and remote user. Keep host-key checking enabled and provision trusted host keys once through the operator's normal process; do not disable checking to avoid sandbox writes. If a release command supplies `-F`, install the block in that file; an explicit `-S`/`-o ControlPath` overrides the file and must be adjusted once to use the same directory. Like any OpenSSH Unix socket path, the expanded path must fit the platform's length limit; unusually long home paths may exceed it.

The first ordinary SSH command creates its master automatically; subsequent SSH/SCP clients in the same sandbox command (for example, one release script or `make` invocation) reuse it. OpenSSH's finite `ControlPersist` closes idle masters and removes their sockets. A sandbox namespace can terminate its background master when the command exits, so reuse is not guaranteed across separate Codex tool commands. After all task descendants have exited, Murphy removes remaining socket files in this project's dedicated directory; other files and symlinks are preserved. Existing cancellation/timeout draining remains in force. No SSH transport, master launcher or connection registry is added.

Every task recreates a missing directory, and its path is stable across Murphy/WSL restarts. OpenSSH automatically replaces a dead control socket on its next connection after an unclean stop. Normal queue reconciliation still applies to interrupted tasks; this change does not replay uncertain commands. No `ssh -MNf`, `ssh -O exit`, per-release operator action, server/firewall change or production change is required.

### Prototype compatibility

The original explicit-JSON, single-project interface remains available via `python3 bridge.py --config PATH ...` from source, or `murphy --config PATH ...` after installation. `config.example.json` contains fictional values only; it is shipped in source and installed under the environment's `share/murphy/`. Fields: `repository`, `repository_id`, `bot_user_id` (publishing account), `owner_user_id`, `owner_login`, and optional `git`, `gh`, `codex` executables. Publishing and owner IDs may now match. Separate legacy bot accounts remain supported.

Existing scoped prototype databases and saved sessions are not migrated into the new manager or modified during installation. Continue them with their original config/state paths. Unscoped older pilot state is rejected. New manager projects use new activation boundaries; do not run both interfaces against the same tasks.

## Development and controlled pilot

```bash
python3 tests/test_bridge.py
python3 tests/test_manager.py
python3 tests/test_auth.py
python3 -m venv .venv
.venv/bin/python -m pip install 'build==1.2.2.post1'
.venv/bin/python tests/check_package.py
```

Behavior tests use temporary Git repositories and a Codex CLI double. They do not call real models, publish to GitHub or require private configuration. The package check builds both distribution formats twice, compares hashes, inspects archive contents and installs each in a fresh environment with an empty HOME. Build tools may be downloaded from the Python package index; GitHub/Codex credentials are unnecessary. GitHub CI runs only after a PR from this repository's `develop` branch is merged into `master`, testing that merge's exact commit. Task PRs and ordinary pushes do not run CI; there is no manual trigger. See [RELEASE.md](RELEASE.md) for exact build/verification commands and promotion steps.

Optional native sandbox check (no model call or GitHub write):

```bash
python3 tests/check_sandbox.py --codex codex
python3 tests/check_sandbox.py --codex codex --separate-account
```

The separate-account sandbox check uses fictional GitHub CLI credentials to exercise real worktree editing/committing, scoped Git credential retrieval and GitHub identity reads inside the native sandbox. It makes no model calls or real GitHub requests.

Optional local OpenSSH check (requires an available OpenSSH server executable for the disposable fixture, never a production server):

```bash
python3 tests/check_ssh.py --codex codex --sshd /usr/sbin/sshd
```

This starts an unprivileged server bound only to `127.0.0.1` with fresh test keys and pinned host keys. Two real Murphy worker processes run start/resume with a CLI double replacing model output; commands run under the native Codex sandbox using the worker's actual permission arguments. It checks cold master creation, SSH and bidirectional SCP reuse with subsequent client authentication disabled, one authenticated connection per turn, idle expiry, worker cleanup, dead-socket recovery, missing-directory recreation, host/user/port scoping, strict host-key rejection and write protection for fixture keys, configuration, sibling source files and another project's socket directory. It makes no model calls or external SSH/GitHub requests and does not reboot WSL. The server executable is a test prerequisite, not a Murphy runtime dependency. Native option references: [Codex permission profiles](https://learn.chatgpt.com/docs/permissions), [OpenSSH client configuration](https://man.openbsd.org/ssh_config).

A controlled live pilot requires explicit authorization: use disposable repositories with your own credentials, a separate state directory and the intended base. For separate-account qualification, also use an authorized second account: verify only owner comments trigger work, reports/PRs/pushes belong to the executor, commit attribution matches, and normal human Git/GitHub settings remain unchanged. Start with the inspection-only example; verify the report and session, then authorize a small edit/test/commit/PR task and a continuation. Confirm a stable UUID per continued task, isolated worktrees, the configured concurrency bound, per-project order and stop/recovery behavior. Exercise the serial override for projects with shared infrastructure. Do not publish, merge or deploy as part of this pilot. This release preparation does not claim a live multi-project/two-account pilot was performed.

Automatic merge/deployment, a broker, dashboard, parallel execution within one project, native Windows support and service packaging are outside this candidate.

## License

MIT. See [LICENSE](LICENSE).
