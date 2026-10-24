"""Conservative worktree retirement, called only by the coordinator under its lock."""
import json
import os
from pathlib import Path
import shutil
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


def beneath(value, root):
    path = Path(value.removesuffix(' (deleted)'))
    return path == root or root in path.parents


def process_use(cwd, proc=Path('/proc')):
    """Inspect this user's processes strictly; report protected foreign processes.

    Foreign UID internals are outside the unprivileged guarantee, not proven unused.
    Visible references from those processes still block cleanup.
    """
    root = Path(cwd)
    limited = []
    # Directory ownership is not a reliable process UID (e.g. non-dumpable tasks).
    # Check all four UIDs so a setuid child of this user remains in the strict scope.
    for process in proc.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            identity = (process / 'status').read_text()
            uids = [int(value) for line in identity.splitlines() if line.startswith('Uid:')
                    for value in line.split()[1:]]
            if len(uids) != 4:
                raise ValueError(f'Cannot establish UID of process {process.name}')
            foreign = not {os.getuid(), os.geteuid()} & set(uids)
            birth = (process / 'stat').read_text().rsplit(')', 1)[1].split()
            if len(birth) < 20:
                raise ValueError(f'Cannot establish birth time of process {process.name}')
            status = birth[0]
            if status in ('Z', 'X'):
                continue
            protected = False
            # Check cmdline even when cwd/fd/maps are protected by ptrace permissions.
            # An inaccessible status/cmdline never establishes an unrelated foreign PID.
            data = (process / 'cmdline').read_bytes()
            if os.fsencode(cwd) in data:
                raise ValueError(f'Process {process.name} references worktree (cmdline)')
            links = [process / 'cwd', process / 'root']
            try:
                links.extend((process / 'fd').iterdir())
            except PermissionError:
                if not foreign:
                    raise
                protected = True
            for link in links:
                try:
                    target = os.readlink(link)
                except FileNotFoundError:
                    if link.parent == process and not foreign:
                        raise ValueError(f'Cannot inspect process {process.name}: missing {link.name}')
                    continue  # FDs can close while inspecting a live process.
                except PermissionError:
                    if not foreign:
                        raise
                    protected = True
                    continue
                if target.startswith('/') and beneath(target, root):
                    raise ValueError(f'Process {process.name} uses worktree ({link.name})')
            try:
                data = (process / 'maps').read_bytes()
            except PermissionError:
                if not foreign:
                    raise
                protected = True
            else:
                if os.fsencode(cwd) in data:
                    raise ValueError(f'Process {process.name} references worktree (maps)')
            current = [int(value) for line in (process / 'status').read_text().splitlines()
                       if line.startswith('Uid:') for value in line.split()[1:]]
            after = (process / 'stat').read_text().rsplit(')', 1)[1].split()
            if len(after) < 20 or current != uids or after[19] != birth[19]:
                raise ValueError(f'Process {process.name} identity changed during inspection')
            if protected:
                limited.append(process.name)
        except (FileNotFoundError, ProcessLookupError) as error:
            if process.exists():
                raise ValueError(f'Cannot inspect live process {process.name}: missing process information') from error
            continue  # Process exited while inspecting it.
        except PermissionError as error:
            raise ValueError(f'Cannot inspect process {process.name}: permission denied') from error
    return limited


def runtime_use(cwd):
    """Optional Docker/Podman CLI; an installed but inaccessible runtime blocks removal."""
    sockets = (Path('/var/run/docker.sock'), Path('/run/podman/podman.sock'),
               Path(f'/run/user/{os.getuid()}/podman/podman.sock'),
               Path.home() / '.docker/run/docker.sock')
    runtimes = [name for name in ('docker', 'podman') if shutil.which(name)]
    if (any(path.exists() for path in sockets) or os.environ.get('DOCKER_HOST')
            or os.environ.get('CONTAINER_HOST')) and not runtimes:
        raise ValueError('Container runtime detected but no inspection CLI is available')
    for runtime in runtimes:
        def run(*args):
            result = subprocess.run([runtime, *args], capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise ValueError(f'Cannot inspect {runtime} runtime: {result.stderr.strip()[:200]}')
            return result.stdout
        endpoint = os.environ.get('DOCKER_HOST' if runtime == 'docker' else 'CONTAINER_HOST')
        if runtime == 'docker' and not endpoint:
            endpoint = json.loads(run('context', 'inspect', '--format', '{{json .Endpoints.docker.Host}}'))
        if endpoint and (not isinstance(endpoint, str) or not endpoint.startswith('unix://')):
            raise ValueError(f'Cannot inspect local containers through remote {runtime} endpoint')
        ids = run('ps', '-aq').split()
        # Inspect individually to bound argv/output and include stopped containers that may restart.
        for ident in ids:
            containers = json.loads(run('inspect', ident))
            if not isinstance(containers, list) or len(containers) != 1:
                raise ValueError(f'Invalid {runtime} inspection response')
            container = containers[0]
            mounts = container.get('Mounts')
            if not isinstance(mounts, list):
                raise ValueError(f'Missing {runtime} mount information')
            for mount in mounts:
                source = mount.get('Source')
                if source and (beneath(str(Path(source).resolve()), Path(cwd))
                               or beneath(cwd, Path(source).resolve())):
                    raise ValueError(f'{runtime} container {ident} mounts worktree ({source})')


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


def remote_saved(cwd, head):
    # Refresh refs, then compare their exact OIDs against a live remote advertisement.
    # A local tracking ref alone is never sufficient, including after branch deletion.
    b.git(cwd, 'fetch', '--no-tags', 'origin', '+refs/heads/*:refs/remotes/origin/*')
    advertised = {tuple(line.split()) for line in b.git(cwd, 'ls-remote', '--heads', 'origin').splitlines()}
    refs = b.git(cwd, 'for-each-ref', '--contains', head,
                 '--format=%(objectname) %(refname)', 'refs/remotes/origin/').splitlines()
    for ref in refs:
        oid, name = ref.split()
        if (oid, 'refs/heads/' + name.removeprefix('refs/remotes/origin/')) in advertised:
            return
    raise ValueError('Commits are not preserved in a freshly confirmed remote branch')


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
            remote_saved(task['cwd'], before['head'])
            limited = process_use(task['cwd'])
            runtime_use(task['cwd'])
            record(state, task, 'removing', 'Removal started; interrupted operations require reconciliation')
            # The coordinator lock excludes executors/registrations. This write transaction
            # also excludes local enqueues between the final busy check and removal.
            with state.db:
                state.db.execute('BEGIN IMMEDIATE')
                if validate(state, task, proof) != before:
                    raise ValueError('Worktree changed during cleanup checks')
                removal_started = True
                b.git(proof['repo'], 'worktree', 'remove', task['cwd'])
                reason = 'Worktree removed; history and session retained'
                if limited:
                    reason += '; protected foreign-UID processes outside inspection guarantee: ' + ', '.join(limited)
                record(state, task, 'removed', reason)
        except (ValueError, KeyError, TypeError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            record(state, task, 'removing' if removal_started else 'blocked',
                   ('Removal outcome requires operator reconciliation; no automatic replay: ' if removal_started else '')
                   + str(error))
