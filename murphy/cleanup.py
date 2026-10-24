"""Retire closed tasks through Git, under the coordinator lock."""
import json
from pathlib import Path
import subprocess
from urllib.parse import urlencode

from . import bridge as b


def automatic_tasks(state):
    """Creation plans are durable provenance for installations predating this feature."""
    config = json.loads(state.setting('autostart') or 'null')
    if not config:
        return []
    tasks = []
    for row in state.db.execute('SELECT * FROM tasks'):
        task = dict(row)
        cleanup = json.loads(task['cleanup_json'] or '{}')
        if cleanup.get('state') == 'removed':
            continue
        proof = json.loads(task['automatic_json'] or 'null')
        if proof is None:
            for command in state.db.execute('SELECT plan FROM commands WHERE task=? AND plan IS NOT NULL', (task['name'],)):
                plan = json.loads(command['plan'])
                saved = json.loads(task['checkout_json'] or '{}')
                if (plan.get('cwd') == task['cwd'] and plan.get('branch') == task['branch']
                        and plan.get('repo') == config['repo'] and task['issue_json'] and saved.get('git_dir')):
                    proof = dict(repo=plan['repo'], cwd=plan['cwd'], branch=plan['branch'],
                                 common_dir=task['common_dir'], git_dir=saved['git_dir'])
                    break
        if proof:
            tasks.append((task, proof))
    return tasks


def record(state, task, status, reason):
    value = dict(state=status, reason=reason)
    previous = json.loads(state.db.execute('SELECT cleanup_json FROM tasks WHERE name=?',
                                         (task['name'],)).fetchone()[0] or '{}')
    if previous.get('state') == status and previous.get('reason') == reason:
        return
    with state.db:
        state.db.execute('UPDATE tasks SET cleanup_json=? WHERE name=?',
                         (json.dumps(dict(value, checked=b.now())), task['name']))


def validate(state, task, proof):
    config = json.loads(state.setting('autostart') or '{}')
    path = Path(task['cwd'])
    expected = Path(config['worktrees']) / task['name']
    if (path != expected or path.is_symlink() or path.resolve() != path
            or proof['cwd'] != str(path) or proof['branch'] != task['branch']
            or proof['repo'] != config['repo'] or task['host'] != b.socket.gethostname()
            or task['common_dir'] != config['common_dir'] or proof['common_dir'] != task['common_dir']):
        raise ValueError('Automatic worktree path/repository/host identity changed')
    source = b.checkout(config['repo'])
    if source['common_dir'] != task['common_dir'] or path == Path(config['repo']):
        raise ValueError('Source checkout identity changed or task is the main checkout')
    other = state.db.execute('SELECT name FROM tasks WHERE cwd=? AND name!=?', (str(path), task['name'])).fetchone()
    if other:
        raise ValueError('Worktree is also registered to another task')
    busy = state.db.execute("""SELECT id,state FROM commands WHERE
        (task=? OR item IN (SELECT number FROM items WHERE task=?))
        AND state IN ('queued','running','blocked','unknown') LIMIT 1""", (task['name'], task['name'])).fetchone()
    if busy:
        raise ValueError(f'Command {busy["id"]} is {busy["state"]}')
    entries = b.git(config['repo'], 'worktree', 'list', '--porcelain', '-z').split('\0\0')
    matches = [set(entry.split('\0')) for entry in entries if f'worktree {path}' in entry.split('\0')]
    try:
        path.lstat()  # Unlike exists(), permission errors are not absence.
    except FileNotFoundError:
        if matches:
            raise ValueError('Worktree directory is absent but Git registration remains; operator reconciliation required')
        git_dir = Path(proof['git_dir'])
        if git_dir.parent != Path(task['common_dir']) / 'worktrees':
            raise ValueError('Missing worktree Git identity changed')
        try:
            git_dir.lstat()
        except FileNotFoundError:
            return None  # Confirmed operator removal; no checkout/header checks in a missing cwd.
        raise ValueError('Worktree directory is absent but Git metadata remains; operator reconciliation required')
    try:
        info = b.checkout(str(path))
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise ValueError('Worktree Git identity changed or cannot be inspected') from error
    if (info['branch'] != task['branch'] or info['common_dir'] != task['common_dir']
            or info['git_dir'] != proof['git_dir'] or info['git_dir'] == info['common_dir']):
        raise ValueError('Worktree Git identity changed')
    expected_entry = {f'worktree {path}', f'branch refs/heads/{task["branch"]}', f'HEAD {info["head"]}'}
    if len(matches) != 1 or not expected_entry <= matches[0] or any(
            value.startswith(('locked', 'prunable')) for value in matches[0]):
        raise ValueError('Worktree registration is missing, locked or changed')
    if info['dirty']:
        raise ValueError('Worktree has staged, unstaged or untracked work')
    return info


def retire(state, github, updates, attempted):
    """Only fresh positive GitHub responses from this refresh can authorize deletion."""
    for task, proof in automatic_tasks(state):
        aliases = [row[0] for row in state.db.execute('SELECT number FROM items WHERE task=?', (task['name'],))]
        if not set(aliases) & set(attempted):
            continue
        if json.loads(task['cleanup_json'] or '{}').get('state') == 'removing':
            record(state, task, 'removing', 'Interrupted cleanup requires operator reconciliation; no automatic replay')
            continue
        removal_started = False
        try:
            if not aliases or any(str(number) not in updates for number in aliases):
                raise ValueError('GitHub state is unavailable or ambiguous for a task alias')
            original = json.loads(task['issue_json'] or '{}').get('number')
            if any(updates[str(number)]['state'] not in ('OPEN', 'CLOSED', 'MERGED') for number in aliases):
                raise ValueError('A task issue/PR alias is unverified')
            github.identity()
            # PRs on this branch also block removal even if not explicitly registered aliases.
            query = urlencode(dict(state='all', head=b.CONFIG.repository.split('/')[0] + ':' + task['branch'], per_page=100))
            pulls = github.api(f'repos/{b.CONFIG.repository}/pulls?{query}', pages=True)
            merged = any(updates[str(number)]['state'] == 'MERGED' for number in aliases)
            open_pull = None
            for pull in pulls:
                if (pull['head']['ref'] != task['branch']
                        or (pull['head'].get('repo') or {}).get('id') != b.CONFIG.repository_id
                        or (pull['base'].get('repo') or {}).get('id') != b.CONFIG.repository_id):
                    raise ValueError('Related PR repository identity is ambiguous')
                if pull['state'] == 'open':
                    open_pull = pull['number']
                elif pull['state'] != 'closed':
                    raise ValueError('Related PR state is ambiguous')
                merged = merged or bool(pull.get('merged_at'))
            if not (updates.get(str(original), {}).get('state') == 'CLOSED' or merged):
                if task['cleanup_json']:
                    record(state, task, 'retained', 'Task has no closed issue or merged PR')
                continue
            if open_pull:
                raise ValueError(f'Related PR #{open_pull} is still open')
            if any(updates[str(number)]['state'] == 'OPEN' and (number != original or not merged)
                   for number in aliases):
                raise ValueError('Another task issue/PR alias is still open')
            before = validate(state, task, proof)
            if before is None:
                with state.db:
                    state.db.execute('BEGIN IMMEDIATE')
                    if validate(state, task, proof) is not None:
                        raise ValueError('Worktree reappeared during absence checks')
                    record(state, task, 'removed', 'Worktree already absent and unregistered; history/session retained')
                continue
            record(state, task, 'removing', 'Removal started; interrupted operations require reconciliation')
            # The coordinator lock excludes executors/registrations. This write transaction
            # also excludes local enqueues between the final busy check and removal.
            with state.db:
                state.db.execute('BEGIN IMMEDIATE')
                if validate(state, task, proof) != before:
                    raise ValueError('Worktree changed during cleanup checks')
                removal_started = True
                b.git(proof['repo'], 'worktree', 'remove', task['cwd'])
                record(state, task, 'removed', 'Worktree removed; branch, history and session retained')
        except (ValueError, KeyError, TypeError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            record(state, task, 'removing' if removal_started else 'blocked',
                   ('Removal outcome requires operator reconciliation; no automatic replay: ' if removal_started else '')
                   + str(error))
