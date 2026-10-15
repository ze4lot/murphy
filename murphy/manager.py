"""One account, isolated project registries and one durable scheduling order.

The prototype executor remains single-project. Only this serial coordinator
switches its configuration; scoped calls restore the active project's context
when polling other projects during a turn. No threads or concurrent executors.
"""
from __future__ import annotations

import contextlib
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

from . import bridge as b


@contextlib.contextmanager
def project_config(config):
    previous = b.CONFIG
    b.CONFIG = config
    try:
        yield
    finally:
        b.CONFIG = previous


def prerequisite(executable):
    if not isinstance(executable, str) or not executable.strip() or any(c in executable for c in '\0\n\r'):
        raise ValueError('Tool must be one executable name or path')
    if shutil.which(executable) is None:
        raise ValueError(f'Missing executable: {executable}. Install it or provide its path during murphy init.')


def platform_check():
    if sys.platform != 'linux' or sys.version_info < (3, 11):
        raise ValueError('Murphy requires Linux/WSL and Python 3.11 or newer.')
    if not hasattr(os, 'pidfd_open') or not Path('/proc/self/stat').exists():
        raise ValueError('Murphy requires Linux /proc and pidfd support.')


class Account:
    def __init__(self, settings):
        self.settings = settings

    def api(self, path, pages=False):
        args = [self.settings['gh'], 'api', '--hostname', 'github.com', path]
        if pages:
            args += ['--paginate', '--slurp']
        result = subprocess.run(args, text=True, capture_output=True, timeout=25)
        if result.returncode:
            raise RuntimeError('GitHub request failed; check gh auth status and repository permissions: '
                               + result.stderr.strip()[:400])
        value = json.loads(result.stdout)
        return [item for page in value for item in page] if pages else value

    def current(self):
        user = self.api('user')
        if type(user.get('id')) is not int or user['id'] <= 0 or user.get('type') != 'User':
            raise ValueError('Sign in to gh with your personal GitHub account first.')
        return user

    def verify(self):
        if self.current()['id'] != self.settings['user_id']:
            raise ValueError('GitHub account changed. Restore the configured account; '
                             'use a separate --state-dir and init for another account.')


class ProjectState(b.State):
    def __init__(self, manager, project_id):
        self.manager = manager
        super().__init__(manager.directory / 'projects' / str(project_id), manager.worker_lock)

    def control(self, verb, target=''):
        self.manager.control(verb, target)

    def finish(self, ident, state, detail):
        super().finish(ident, state, detail)
        if state == 'unknown':
            self.manager.set_paused(True)


class ProjectGitHub(b.GitHub):
    def __init__(self, manager, project_id):
        self.manager = manager
        self.project_id = project_id

    def api(self, *args, **kwargs):
        # Recheck before every read/write, including reports and source revalidation.
        self.manager.account.verify()
        return super().api(*args, **kwargs)

    def report(self, row):
        self.identity()
        row = dict(row)
        row['id'] = self.manager.ticket(self.project_id, row['id'])
        return super().report(row)


class ProjectWorker(b.Worker):
    def __init__(self, manager, *args, **kwargs):
        self.manager = manager
        super().__init__(*args, **kwargs)

    def poll(self):
        self.manager.pump()

    def verify_comment(self, row):
        # A repository deleted/replaced under the same name is a new identity.
        self.github.identity()
        super().verify_comment(row)

    def controls(self):
        # pump handles controls from all projects, also during an active turn.
        pass


class Manager:
    def __init__(self, directory=None):
        platform_check()
        self.directory = Path(directory or Path.home() / '.local/state/murphy/manager').expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.worker_lock = Path.home() / '.local/state/murphy/worker.lock'
        self.worker_lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / 'manager.sqlite', timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY, repository TEXT NOT NULL UNIQUE COLLATE NOCASE,
                config TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project INTEGER NOT NULL REFERENCES projects(id), command INTEGER NOT NULL,
                UNIQUE(project, command)
            );
        ''')
        saved = self.setting('account')
        self.account = Account(json.loads(saved)) if saved else None
        self.projects = {}
        self.stop = False
        self.poll_seconds = 30
        self.timeout = 600
        self.last_identity_check = 0

    def close(self):
        for _, state, _ in self.projects.values():
            state.db.close()
        self.db.close()

    def setting(self, key):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set_paused(self, paused):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('paused', str(int(paused))))

    def initialized(self):
        if self.account is None:
            raise ValueError('Run murphy init first (after gh auth login and codex login).')

    def init(self, gh='gh', git='git', codex='codex'):
        if self.account is not None:
            raise ValueError('Already initialized. The saved GitHub identity will not be replaced.')
        settings = {'gh': gh, 'git': git, 'codex': codex}
        for executable in settings.values():
            prerequisite(executable)
        account = Account(settings)
        user = account.current()
        settings.update(user_id=user['id'], login=user['login'])
        with self.db:
            self.db.execute('INSERT INTO settings VALUES (?, ?)', ('account', json.dumps(settings)))
            self.db.execute('INSERT INTO settings VALUES (?, ?)', ('paused', '0'))
        self.account = Account(settings)
        return user['login']

    def load(self):
        self.initialized()
        for row in self.db.execute('SELECT * FROM projects ORDER BY id').fetchall():
            if row['id'] in self.projects:
                continue
            config = b.Config(**json.loads(row['config']))
            state = ProjectState(self, row['id'])
            try:
                state.bind_config(config)
                state.initialized()
            except Exception:
                state.db.close()
                raise
            worker = ProjectWorker(self, state, ProjectGitHub(self, row['id']),
                                   codex=config.codex, poll_seconds=self.poll_seconds, timeout=self.timeout)
            self.projects[row['id']] = config, state, worker

    def available(self):
        self.initialized()
        self.account.verify()
        return [{'repository': repo['full_name'], 'can_push': repo.get('permissions', {}).get('push', False)}
                for repo in self.account.api('user/repos?per_page=100&sort=full_name', pages=True)
                if not repo.get('archived') and not repo.get('disabled')]

    def project(self, repository):
        self.load()
        for config, state, worker in self.projects.values():
            if config.repository.casefold() == repository.casefold():
                return config, state, worker
        raise ValueError('Project is not connected; use murphy projects add first.')

    def register(self, repository, name, cwd, branch, items, model=None, reasoning=None):
        config, state, _ = self.project(repository)
        with b.lock(self), project_config(config):
            state.register(name, cwd, branch, items, model, reasoning)

    def enqueue(self, repository, task, verb, prompt):
        _, state, _ = self.project(repository)
        if state.db.execute('SELECT 1 FROM tasks WHERE name=?', (task,)).fetchone() is None:
            raise ValueError('Task is not registered in this project.')
        command = state.enqueue(task, verb, prompt)
        self.sync_queue()
        project_id = next(key for key, (_, registered, _) in self.projects.items() if registered is state)
        return self.ticket(project_id, command)

    def ingest(self, repository, comment_id):
        config, state, worker = self.project(repository)
        with project_config(config):
            worker.github.identity()
            commands = state.ingest([worker.github.comment(comment_id)])
        self.sync_queue()
        return [self.ticket(config.repository_id, command) for command in commands]

    def add(self, repository, checkout=None, base=None, model=None):
        self.initialized()
        if not b.re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Use a GitHub owner/repository name.')
        self.account.verify()
        repo = self.account.api('repos/' + repository)
        if repo.get('archived') or repo.get('disabled') or not repo.get('permissions', {}).get('push'):
            raise ValueError('Choose an active repository where your gh account has push access.')
        repository = repo['full_name']
        if self.db.execute('SELECT 1 FROM projects WHERE id=? OR repository=?', (repo['id'], repository)).fetchone():
            raise ValueError('Project is already connected; its activation boundary has been preserved.')
        settings = self.account.settings
        config = b.Config(repository, repo['id'], settings['user_id'], settings['user_id'], settings['login'],
                          git=settings['git'], gh=settings['gh'], codex=settings['codex'])
        for tool in (config.git, config.gh, config.codex):
            prerequisite(tool)
        if not base:
            base = repo.get('default_branch')
        if not base:
            raise ValueError('Repository has no default branch; push an initial commit first.')
        state = ProjectState(self, repo['id'])
        try:
            with project_config(config):
                github = ProjectGitHub(self, repo['id'])
                # Capture activation before cloning; never import historical commands.
                if state.setting('baseline') is None:
                    state.bind_config(config)
                    state.initialize(github)
                else:
                    state.bind_config(config)
                if checkout:
                    source = Path(checkout).expanduser().resolve()
                else:
                    source = self.directory / 'checkouts' / str(repo['id'])
                    if source.exists():
                        raise ValueError('Managed checkout already exists; inspect it and use --checkout explicitly.')
                    source.parent.mkdir(parents=True, exist_ok=True)
                    # gh uses the selected account and its configured Git protocol.
                    self.account.verify()
                    subprocess.run([config.gh, 'repo', 'clone', 'https://github.com/' + repository, str(source)],
                                   check=True, timeout=300)
                b.checkout(str(source))
                b.git(str(source), 'check-ref-format', '--branch', base)
                b.git(str(source), 'fetch', 'origin', base)
                b.git(str(source), 'rev-parse', '--verify', f'refs/remotes/origin/{base}^{{commit}}')
                latest = github.latest()
                state.configure_autostart(source, state.directory / 'worktrees', base, model,
                                          latest['id'] if latest else 0)
            with self.db:
                self.db.execute('INSERT INTO projects VALUES (?, ?, ?)',
                                (repo['id'], repository, json.dumps(asdict(config))))
        finally:
            state.db.close()
        self.load()
        return repository

    def sync_queue(self):
        # A committed project command missing from this index after a crash is
        # enrolled, never re-created. Existing ticket numbers/order never change.
        pending = []
        for project, (_, state, _) in self.projects.items():
            enrolled = self.db.execute('SELECT COALESCE(MAX(command),0) FROM queue WHERE project=?',
                                       (project,)).fetchone()[0]
            for row in state.db.execute('SELECT * FROM commands WHERE id>? ORDER BY id', (enrolled,)):
                snapshot = json.loads(row['snapshot']) if row['snapshot'] else {}
                pending.append((snapshot.get('created_at', row['started'] or ''),
                                snapshot.get('id', 0), project, row['id']))
        with self.db:
            for _, _, project, command in sorted(pending):
                self.db.execute('INSERT OR IGNORE INTO queue(project,command) VALUES (?,?)', (project, command))

    def ticket(self, project, command):
        return self.db.execute('SELECT id FROM queue WHERE project=? AND command=?', (project, command)).fetchone()[0]

    def command(self, ticket):
        row = self.db.execute('SELECT * FROM queue WHERE id=?', (ticket,)).fetchone()
        if row is None:
            raise ValueError('Unknown global command ID.')
        config, state, worker = self.projects[row['project']]
        return config, state, worker, state.row(row['command'])

    def unknown(self):
        return any(state.db.execute("SELECT 1 FROM commands WHERE state='unknown'").fetchone()
                   for _, state, _ in self.projects.values())

    def control(self, verb, target=''):
        if verb == 'pause':
            self.set_paused(True)
        elif verb == 'resume':
            if self.unknown():
                raise ValueError('Resolve unknown outcomes in every project before resuming.')
            self.set_paused(False)
        elif verb == 'cancel':
            _, state, _, row = self.command(int(target))
            # Call the original local implementation; public IDs are global.
            b.State.control(state, 'cancel', str(row['id']))

    def pump(self):
        if self.stop:
            return
        if time.monotonic() - self.last_identity_check >= self.poll_seconds:
            self.last_identity_check = time.monotonic()
            try:
                self.account.verify()
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                print(f'Account check deferred; queue retained: {error}', file=sys.stderr)
                return
        for config, _, worker in self.projects.values():
            with project_config(config):
                b.Worker.poll(worker)
        self.sync_queue()
        # Process controls in the same persisted order as ordinary operations.
        for ticket in self.db.execute('SELECT id FROM queue ORDER BY id').fetchall():
            config, state, worker, row = self.command(ticket['id'])
            if row['state'] == 'queued' and row['verb'] in ('pause', 'resume', 'cancel'):
                with project_config(config):
                    try:
                        worker.verify_comment(row)
                        self.control(row['verb'], row['prompt'])
                        state.finish(row['id'], 'succeeded', 'Controller operation; no model invoked')
                    except Exception as error:
                        state.finish(row['id'], 'failed', str(error))

    def reports(self):
        for config, _, worker in self.projects.values():
            with project_config(config):
                worker.reports()

    def tick(self, fd):
        self.pump()
        self.reports()
        if self.stop or self.setting('paused') == '1' or self.unknown():
            return
        for ticket in self.db.execute('SELECT id FROM queue ORDER BY id').fetchall():
            config, _, worker, row = self.command(ticket['id'])
            if row['state'] == 'queued':
                with project_config(config):
                    worker.execute(row, fd)
                return

    def request_stop(self, *_):
        self.stop = True
        for _, _, worker in self.projects.values():
            worker.stop = True

    def run(self, once=False):
        self.initialized()
        self.account.verify()
        for name in ('git', 'gh', 'codex'):
            prerequisite(self.account.settings[name])
        self.load()
        if not self.projects:
            raise ValueError('Connect a project with murphy projects add OWNER/REPOSITORY first.')
        b.subreaper()
        with b.lock(self) as fd:
            for _, _, worker in self.projects.values():
                worker.recover()
            self.sync_queue()
            previous = {sig: signal.signal(sig, self.request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                while not self.stop:
                    self.tick(fd)
                    if once:
                        self.reports()
                        break
                    time.sleep(0.2)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        return 2 if self.unknown() else 0

    def status(self):
        self.load()
        self.sync_queue()
        projects = [dict(row) for row in self.db.execute('SELECT id,repository FROM projects ORDER BY id')]
        commands = []
        for ticket in self.db.execute('SELECT * FROM queue ORDER BY id').fetchall():
            config, _, _, row = self.command(ticket['id'])
            commands.append(dict(row, id=ticket['id'], repository=config.repository))
        return {'account': self.account.settings['login'], 'paused': self.setting('paused') == '1',
                'projects': projects, 'commands': commands}

    def resolve(self, ticket, outcome, note):
        self.load()
        self.sync_queue()
        with b.lock(self):
            _, state, _, row = self.command(ticket)
            if row['state'] != 'unknown':
                raise ValueError('Only an unknown outcome can be resolved; no replay is performed.')
            state.finish(row['id'], outcome, 'Operator reconciliation: ' + note)
