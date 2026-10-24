"""Retire closed tasks through Git, under the coordinator lock."""
import json
import csv
import io
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


def record(state, task, status, reason, resources=None):
    value = dict(state=status, reason=reason)
    previous = json.loads(state.db.execute('SELECT cleanup_json FROM tasks WHERE name=?',
                                         (task['name'],)).fetchone()[0] or '{}')
    if resources is not None or 'resources' in previous:
        value['resources'] = resources if resources is not None else previous['resources']
    if all(previous.get(key) == item for key, item in value.items()):
        return
    with state.db:
        state.db.execute('UPDATE tasks SET cleanup_json=? WHERE name=?',
                         (json.dumps(dict(value, checked=b.now())), task['name']))


TASK_LABEL = 'io.murphy.worktree'
PROJECT_LABEL = 'com.docker.compose.project'
DIRECTORY_LABEL = 'com.docker.compose.project.working_dir'


def runtime_call(runtime, *args):
    # Pin Docker to the inspected local endpoint, regardless of subsequent context changes.
    env = dict(os.environ)
    env.pop('DOCKER_CONTEXT', None)
    result = subprocess.run([*runtime, *args], capture_output=True, text=True, timeout=25, env=env)
    if result.returncode:
        raise ValueError(f'Container cleanup failed ({args[0]}): {result.stderr.strip()[:300]}')
    return result.stdout.strip()


def task_path(value, cwd):
    if not value or not Path(value).is_absolute():
        return False
    path, root = Path(value).resolve(), Path(cwd)
    return path == root or root in path.parents


def docker_runtime(executable):
    context = os.environ.get('DOCKER_CONTEXT')
    endpoint = os.environ.get('DOCKER_HOST') if not context else None
    if not endpoint:
        endpoint = json.loads(runtime_call([executable], 'context', 'inspect',
            *([context] if context else []), '--format', '{{json .Endpoints.docker.Host}}'))
    if not isinstance(endpoint, str) or not endpoint.startswith('unix://'):
        raise ValueError('Task container cleanup requires a local Docker endpoint')
    return [executable, '--host', endpoint]


def writable_directories(cwd):
    def inaccessible(error):
        raise error
    try:
        for directory, _, _ in os.walk(cwd, onerror=inaccessible, followlinks=False):
            if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
                return False
    except PermissionError:
        return False
    return True


def repair_permissions(cwd):
    """Repair container-created directories through a single task-only bind mount."""
    if writable_directories(cwd):
        return
    executable = shutil.which('docker')
    if not executable:
        raise ValueError('Container-owned task files require local Docker for ownership repair')
    runtime = docker_runtime(executable)
    mount = io.StringIO()
    csv.writer(mount, lineterminator='').writerow([
        'type=bind', 'source=' + cwd, 'target=/worktree', 'bind-recursive=disabled'])
    runtime_call(runtime, 'run', '--rm', '--pull=missing', '--network=none', '--read-only',
                 '--user=0', '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=DAC_OVERRIDE',
                 '--cap-add=FOWNER', '--security-opt=no-new-privileges', '--label', TASK_LABEL + '=' + cwd,
                 '--mount', mount.getvalue(), '--entrypoint=/bin/sh', 'alpine:3.22', '-ec',
                 'chown -hR "$1" /worktree; find /worktree -xdev -type d -exec chmod u+rwx {} +',
                 '--', str(os.getuid()) + ':' + str(os.getgid()))
    if not writable_directories(cwd):
        raise ValueError('Task directories remain inaccessible after container ownership repair')


def container_plan(cwd):
    """Inventory only task-owned local containers and their private resources."""
    plans = []
    for name in ('docker', 'podman'):
        executable = shutil.which(name)
        if not executable:
            sockets = ((Path('/var/run/docker.sock'), Path(f'/run/user/{os.getuid()}/docker.sock'),
                        Path.home() / '.docker/run/docker.sock')
                       if name == 'docker' else (Path(f'/run/user/{os.getuid()}/podman/podman.sock'),))
            if any(path.exists() for path in sockets) or os.environ.get(
                    'DOCKER_HOST' if name == 'docker' else 'CONTAINER_HOST'):
                raise ValueError(name + ' runtime detected but cleanup CLI is unavailable')
            continue
        if name == 'docker':
            runtime = docker_runtime(executable)
        else:
            runtime = [executable, '--remote=false']
        ids = runtime_call(runtime, 'ps', '-aq', '--no-trunc').split()
        containers = []
        for offset in range(0, len(ids), 50):
            batch = ids[offset:offset + 50]
            info = json.loads(runtime_call(runtime, 'container', 'inspect', *batch))
            if (not isinstance(info, list) or len(info) != len(batch)
                    or any(not isinstance(item, dict) for item in info)
                    or {item.get('Id') for item in info} != set(batch)):
                raise ValueError('Invalid container inspection response')
            containers.extend(info)
        owned, projects = [], set()
        for container in containers:
            labels = container['Config'].get('Labels') or {}
            owners = [labels[key] for key in (TASK_LABEL, DIRECTORY_LABEL) if labels.get(key)]
            bound = any(mount.get('Type') == 'bind' and task_path(mount.get('Source'), cwd)
                        for mount in container['Mounts'])
            if not (any(task_path(owner, cwd) for owner in owners) or bound):
                continue
            if any(not task_path(owner, cwd) for owner in owners):
                raise ValueError('Container has conflicting task ownership: ' + container['Id'])
            owned.append(container['Id'])
            if labels.get(PROJECT_LABEL):
                projects.add(labels[PROJECT_LABEL])
        for container in containers:
            labels = container['Config'].get('Labels') or {}
            if labels.get(PROJECT_LABEL) in projects and container['Id'] not in owned:
                raise ValueError('Compose project is shared with another worktree: ' + labels[PROJECT_LABEL])
        plan = dict(runtime=runtime, containers=owned, networks=[], volumes=[])
        # Label filters also find explicitly tagged resources after all containers exited.
        filters = [TASK_LABEL + '=' + cwd, *[PROJECT_LABEL + '=' + project for project in sorted(projects)]]
        for kind, key in (('network', 'networks'), ('volume', 'volumes')):
            resources = set()
            for label in filters:
                resources.update(runtime_call(runtime, kind, 'ls', '-q', '--filter', 'label=' + label).split())
            for ident in sorted(resources):
                response = json.loads(runtime_call(runtime, kind, 'inspect', ident))
                if (not isinstance(response, list) or len(response) != 1
                        or not isinstance(response[0], dict)):
                    raise ValueError('Invalid container resource inspection response')
                info = response[0]
                if name == 'podman' and kind == 'network':
                    info = dict(Name=info['name'], Id=info['id'], Labels=info.get('labels'),
                                Containers=info.get('containers') or info.get('Containers') or {})
                labels = info.get('Labels') or {}
                if not (task_path(labels.get(TASK_LABEL), cwd) or labels.get(PROJECT_LABEL) in projects):
                    raise ValueError('Container resource ownership changed during inspection')
                if labels.get(TASK_LABEL) and not task_path(labels[TASK_LABEL], cwd):
                    raise ValueError('Container resource has conflicting task ownership')
                # Never remove a resource used by another task, including stopped containers.
                foreign = [container for container in containers if container['Id'] not in owned]
                if kind == 'volume':
                    shared = any(mount.get('Name') == info['Name'] for container in foreign
                                 for mount in container['Mounts'])
                    identity = dict(name=info['Name'], created=info['CreatedAt'], labels=labels)
                else:
                    shared = any(info['Name'] in (container.get('NetworkSettings', {}).get('Networks') or {})
                                 for container in foreign)
                    shared = shared or bool(set(info.get('Containers', {})) - set(owned))
                    identity = info.get('Id') or info['Name']
                if not shared:
                    plan[key].append(identity)
        if any(plan[key] for key in ('containers', 'networks', 'volumes')):
            plans.append(plan)
    return plans


def remove_containers(plans):
    """Remove recorded task resources before deleting their source directory."""
    for plan in plans:
        runtime = plan['runtime']
        for ident in plan['containers']:
            runtime_call(runtime, 'container', 'rm', '--force', '--volumes', ident)
        for ident in plan['networks']:
            runtime_call(runtime, 'network', 'rm', ident)
        for ident in plan['volumes']:
            response = json.loads(runtime_call(runtime, 'volume', 'inspect', ident['name']))
            if (not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict)
                    or response[0].get('CreatedAt') != ident['created']
                    or (response[0].get('Labels') or {}) != ident['labels']):
                raise ValueError('Task volume identity changed before removal')
            runtime_call(runtime, 'volume', 'rm', ident['name'])


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
            resources = container_plan(task['cwd'])
            record(state, task, 'removing', 'Removal started; interrupted operations require reconciliation', resources)
            # The coordinator lock excludes executors/registrations. This write transaction
            # also excludes local enqueues between the final busy check and removal.
            with state.db:
                state.db.execute('BEGIN IMMEDIATE')
                if validate(state, task, proof) != before:
                    raise ValueError('Worktree changed or reappeared during cleanup checks')
                removal_started = True
                remove_containers(resources)
                if before is not None:
                    repair_permissions(task['cwd'])
                if container_plan(task['cwd']):
                    raise ValueError('Task container resources remain after cleanup')
                if before is not None:
                    b.git(proof['repo'], 'worktree', 'remove', task['cwd'])
                reason = 'Worktree removed' if before is not None else 'Worktree already absent and unregistered'
                record(state, task, 'removed', reason + '; task containers removed; branch, history and session retained')
        except (ValueError, KeyError, TypeError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            record(state, task, 'removing' if removal_started else 'blocked',
                   ('Removal outcome requires operator reconciliation; no automatic replay: ' if removal_started else '')
                   + str(error))
