# murphy

An experimental local bridge from GitHub issue/PR comments to persistent Codex CLI sessions.

Murphy polls for explicit commands from one configured human account, stores them in SQLite, and executes one operation at a time. A new issue can get a separate branch, worktree and Codex session. Continuations resume the saved session UUID and reasoning setting. Results are posted back to GitHub by a separate bot account.

The executor can **edit code, run tests, commit, push its task branch, and open or update a PR** when the authenticated owner's task calls for it. It follows the target repository's `AGENTS.md` and explicit task instructions. Merge, deployment, releases and changes to other projects are not implied by a normal implementation request.

Codex runs with a named permission profile extending `:workspace`: the task worktree and its Git metadata are writable, network access is enabled, and interactive approval prompts are disabled. Linked worktrees need access to both their own Git index and the shared Git refs/objects. Murphy grants those metadata directories explicitly, not the source checkout's working files. This uses Codex permission profiles (tested with CLI 0.162.1), without a full-access fallback; see [OpenAI's permission profile documentation](https://learn.chatgpt.com/docs/permissions). External CLI tools still use the credentials and permissions supplied by the installation.

## Requirements

- Linux/WSL and Python 3.11+; the prototype was tested on Python 3.12. It uses Linux `/proc`, pidfds and child-subreaper support.
- `git`, GitHub CLI (`gh`) and Codex CLI available locally.
- A GitHub bot account with access to the configured repository, permission to post issue comments, and push/PR access for development tasks. Configure Git authentication/author identity for that agent account as well.
- A separately authenticated Codex CLI installation. Model access is supplied by the user's own account.

The bridge uses only Python's standard library. No pip/npm installation, application database, Docker stack, webhook receiver or external relay is required. WakeWire is not a dependency.

## Configuration

Copy the example and replace **all example identities** before using it:

```bash
cp config.example.json config.local.json
```

`config.local.json` is ignored by Git. The example contains fictional values, not a usable installation or credentials.

| Field | Meaning |
| --- | --- |
| `repository` | Canonical, case-preserving GitHub `owner/repository` name. |
| `repository_id` | Immutable numeric repository ID. |
| `bot_user_id` | Immutable numeric ID of the account authenticated by `gh`. |
| `owner_user_id` | Immutable numeric ID of the human allowed to submit commands. |
| `owner_login` | Login of that human; startup verifies it against the numeric ID. |
| `git`, `gh`, `codex` | Optional executable names or absolute paths; defaults are `git`, `gh`, `codex`. These are single executables, not shell command strings. |

For example, after authenticating GitHub CLI as your bot, inspect IDs with:

```bash
gh api user --jq .id
gh api repos/OWNER/REPOSITORY --jq '{id, full_name, default_branch}'
gh api users/HUMAN_LOGIN --jq '{id, login, type}'
```

Keep authentication in the respective CLI tools, **not in this JSON or the repository**. To isolate a bot login from your human account, the `gh` setting can point to your own external wrapper using a separate GitHub CLI configuration. The bridge verifies the bot's immutable ID before network operations; it does not silently switch accounts. Git wrappers are also supported. No credentials or machine-specific wrappers are distributed here.

One process is configured for one repository. Its default state directory is `~/.local/state/murphy/<owner>/<repository>`; `--state-dir` can override it. State is pinned to the repository and authorization identities, so a different project cannot silently reuse the same registry or issue numbers. Use a **fresh state directory** for this extracted prototype; unscoped state from earlier private pilots is rejected rather than guessed or migrated.

## Start a task

```bash
python3 bridge.py --config config.local.json init
python3 bridge.py --config config.local.json configure-autostart \
  --repo /absolute/path/to/your/existing-checkout --base main
python3 bridge.py --config config.local.json run
```

Set `--base` to the actual base branch of your repository. `init` captures an activation boundary, so old comments do not become commands. Enabling autostart also captures its own boundary; updating its configuration does not reset it.

From the configured human account, post a **new** issue comment:

```text
/murphy start
Implement the change described in this issue, run the relevant tests and open a draft PR.
```

The command belongs on its own first line; instructions begin on the next line. LF/CRLF and incidental spaces around the command are accepted. Inline instructions, quoted commands and four-space/tab code blocks are rejected. Editing an existing comment does not activate it.

For an inspection-only task, explicitly ask for no changes. The bridge no longer imposes a blanket read-only instruction on development tasks. Its prompt supplies the configured Git/GitHub executable paths so the agent can use the installation's intended identity.

For an unregistered open issue, the worker snapshots the title/body and creates `codex/issue-<number>-murphy` in `<state>/worktrees/issue-<number>` before starting a new session. A direct parent with one unambiguous issue-specific open PR can supply its feature branch as the base; otherwise the configured base is used. The source checkout is not switched, cleaned or copied. Existing issue branches/worktree paths stop automatic creation; they are never overwritten or silently adopted. Automatic creation targets issues, not PRs.

To continue, post another comment:

```text
/murphy continue
Continue the implementation and address the feedback above.
```

A continuation uses the saved session and can resume uncommitted work left by the previous turn. Murphy saves a checkout checkpoint after each execution: branch/Git directories, HEAD, and a fingerprint of tracked/index changes and untracked file contents. Before the next turn it compares the checkout with that checkpoint. Changes made between turns outside Murphy pause the task without overwriting them; unchanged dirty work from the agent is accepted. This detects between-turn changes, not the author of simultaneous human edits during an active turn. Older tasks without a checkpoint must be clean on their first execution with this version. A continuation never creates a replacement session if the binding is missing. A repeated start fails without replacing the existing session. Issue/PR aliases can be bound to the same task through manual registration.

### Reasoning

New sessions default to **high**. To override it, put exactly one plain line in the issue body, outside code fences or quotes:

```text
Reasoning: medium
```

Accepted values are `low`, `medium`, `high`, `xhigh`, `max`, `ultra`; the chosen model must support the value. Invalid or duplicate metadata stops the start before branch creation/model invocation. The selected level is stored and explicitly forwarded on both start and resume. Later edits to the issue do not change that session's recorded settings. Legacy existing sessions retain their settings.

Model selection is optional through `configure-autostart --model MODEL` or `register --model MODEL`; otherwise Codex CLI chooses its default. The bridge does not infer reasoning or select a model through another model. Custom GitHub fields and local-model routing are not implemented.

### Manual registration

For a pre-existing clean worktree on the named branch:

```bash
python3 bridge.py --config config.local.json register example-task \
  --cwd /absolute/path/to/task-worktree --branch codex/task-branch \
  --item 123 --item 124 --reasoning high
```

Each `--item` is an issue or PR number in the configured repository. Registration does not infer aliases or create a worktree. The first `start` creates the task's session; subsequent `continue` commands resume it.

## Queue, controls and recovery

One inherited file lock at `~/.local/state/murphy/worker.lock` covers the worker and its Codex descendants, even with different `--state-dir` values. Detached descendants must exit before the slot is released. Only one Murphy worker can run per OS user. This initial version is **not a concurrent multi-repository dispatcher**: use one configured repository/worker at a time. Direct Codex desktop/terminal runs outside Murphy are not governed by this lock.

While idle, the poller only calls GitHub, not a model. Defaults are a 30-second poll interval and a 600-second execution timeout. `run --poll-seconds 5 --timeout 180` changes them; `run --once` performs one scheduling pass. Stop the foreground worker with Ctrl+C; active descendants are cancelled and drained. Partial edits and commits are preserved; cancellation does not roll them back. No service or autostart is installed.

Controller comments must occupy the entire comment:

```text
/murphy pause
```

```text
/murphy resume
```

```text
/murphy cancel 12
```

`cancel` uses the local command ID in a report. Pause lets current work finish, then holds the queue. Controls remain available during an active turn and do not call a model. Local equivalents are also available:

```bash
python3 bridge.py --config config.local.json status
python3 bridge.py --config config.local.json pause
python3 bridge.py --config config.local.json cancel 12
python3 bridge.py --config config.local.json resume
```

Before execution the source comment is refetched; changed, deleted or unavailable sources cancel that queued operation. Source IDs deduplicate commands. A publication error retries only the report; its execution marker reconciles an uncertain POST without repeating model work.

An unclean worker exit, timeout, unexpected checkout/session or uncertain execution produces `unknown` and pauses the queue. Never automatically replay that prompt. Inspect the recorded execution JSONL/stderr, exact session and surviving processes before explicitly resolving it with the worker stopped:

```bash
python3 bridge.py --config config.local.json resolve 12 \
  --outcome failed --note 'Inspected the execution and confirmed that all descendants exited.'
python3 bridge.py --config config.local.json resume
```

Resolution changes bookkeeping only. A branch/worktree creation plan is persisted before creation; partial/uncertain creation preserves that plan and worktree for inspection. `run --once` exits with status 2 when unknown outcomes remain.

A trusted local operator can also use `enqueue TASK start|continue --prompt TEXT`; it uses the same queue. `ingest-comment ID` explicitly refetches a comment through the same activation, author and deduplication gates. Neither command is a way to impersonate a GitHub user.

State and logs can contain issue text and model output. Keep them private and outside version control. The tool uses the existing Codex login without copying auth files, removes inherited OpenAI API-key variables from the child, and ignores unrelated Codex user configuration. Permission and approval settings are passed explicitly on both start and resume. For CLI versions that omit turn IDs from JSON events, it examines only the registered thread's exact rollout file, verifying its UUID/cwd/source and execution time bounds. An unavailable ID is reported as unavailable.

## Development

```bash
python3 tests/test_bridge.py
```

Tests use temporary Git repositories/worktrees and an executable CLI double. They do not call a real model, publish GitHub comments or need your installation config/authentication. Process tests require Linux. The core was exercised locally with Python 3.12 and Codex CLI 0.162.1; broader version/platform compatibility is not qualified.

For an optional check against your installed Codex sandbox, without a model call or GitHub write:

```bash
python3 tests/check_sandbox.py --codex codex
```

It checks real worktree editing, a Git commit, local HTTP access and protection of a sibling source checkout. The ordinary behavior suite also exercises real local edits/commits and a push to a temporary bare repository through the CLI double. A real-model pilot verified start→uncommitted edit→continue→test→commit in one native session; it did not publish a GitHub PR or push to a real remote.

The current scope is one configured repository, explicit human commands, a durable serial queue and a development-capable Codex executor. Automatic merge/deployment, a local broker, a dashboard and service packaging belong to future work.

## License

MIT. See [LICENSE](LICENSE).
