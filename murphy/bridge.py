#!/usr/bin/env python3
"""Foreground GitHub/Codex task executor. Linux + Python stdlib; no daemon installation."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

from .auth import check_git_headers, environment

@dataclass(frozen=True)
class Config:
    repository: str
    repository_id: int
    bot_user_id: int
    owner_user_id: int
    owner_login: str
    git: str = 'git'
    gh: str = 'gh'
    codex: str = 'codex'
    gh_config_dir: str | None = None
    git_name: str | None = None
    git_email: str | None = None


CONFIG: Config | None = None


def configure(path):
    """Load non-secret installation settings; authentication stays in the CLI tools."""
    global CONFIG
    with Path(path).expanduser().open() as handle:
        data = json.load(handle)
    try:
        config = Config(**data)
    except TypeError as error:
        raise ValueError('Invalid configuration fields; see config.example.json') from error
    if not isinstance(config.repository, str) or not re.fullmatch(
            r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+', config.repository):
        raise ValueError('repository must be a GitHub owner/repository name')
    if config.repository.split('/')[1] in ('.', '..'):
        raise ValueError('Invalid repository name')
    if not isinstance(config.owner_login, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', config.owner_login):
        raise ValueError('owner_login must be a GitHub login')
    for field in ('repository_id', 'bot_user_id', 'owner_user_id'):
        value = getattr(config, field)
        if type(value) is not int or value <= 0:
            raise ValueError(field + ' must be a positive integer')
    for field in ('git', 'gh', 'codex'):
        value = getattr(config, field)
        if not isinstance(value, str) or not value.strip() or any(c in value for c in ('\0', '\n', '\r')):
            raise ValueError(field + ' must be one executable name or path, not shell arguments')
    if config.gh_config_dir is not None:
        if (not isinstance(config.gh_config_dir, str) or not Path(config.gh_config_dir).is_absolute()
                or any(c in config.gh_config_dir for c in ('\0', '\n', '\r'))):
            raise ValueError('gh_config_dir must be an absolute private GitHub CLI directory')
        for field in ('git_name', 'git_email'):
            value = getattr(config, field)
            if not isinstance(value, str) or not value.strip() or any(c in value for c in ('\0', '\n', '\r')):
                raise ValueError(field + ' is required for isolated executor Git operations')
    CONFIG = config
    return config


EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')
DEFAULT_REASONING = 'high'
TURN_OUTCOMES = ('succeeded', 'waiting_for_user')
TURN_SCHEMA = {
    'type': 'object',
    'properties': {'outcome': {'type': 'string', 'enum': list(TURN_OUTCOMES)},
                   'message': {'type': 'string'}},
    'required': ['outcome', 'message'],
    'additionalProperties': False,
}


def turn_result(text):
    result = json.loads(text)
    if (not isinstance(result, dict) or set(result) != {'outcome', 'message'}
            or result['outcome'] not in TURN_OUTCOMES or not isinstance(result['message'], str)
            or not result['message'].strip()):
        raise ValueError('Expected an explicit outcome and a nonempty message')
    return result['outcome'], result['message'].strip()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def issue_reasoning(body):
    """Read one plain metadata line, ignoring quoted/fenced/indented examples."""
    values = []
    fence = None
    for line in body.replace('\r\n', '\n').splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            continue
        if fence or line.startswith(('    ', '\t')):
            continue
        match = re.fullmatch(r' {0,3}Reasoning:\s*(.*?)\s*', line, re.IGNORECASE)
        if match:
            values.append(match[1].lower())
    if len(values) > 1:
        raise ValueError('Use exactly one Reasoning: value in the issue body')
    if values and values[0] not in EFFORTS:
        raise ValueError('Invalid Reasoning: choose ' + ', '.join(EFFORTS))
    return values[0] if values else None


def command(body):
    """One complete comment: command alone on line one, optional prompt below."""
    body = body.replace('\r\n', '\n')
    first, separator, rest = body.partition('\n')
    if first.startswith(('    ', '\t')):
        return None
    body = first.strip(' ') + separator + rest
    match = re.fullmatch(r'/murphy (start|continue)(?:\n([^\x00]{1,16000}))?', body)
    if match:
        default = ('Read the registered issue context and carry out its task.'
                   if match[1] == 'start' else 'Continue the registered task.')
        return match[1], (match[2] or default).strip() or default
    if body in ('/murphy pause', '/murphy resume'):
        return body.split()[1], ''
    match = re.fullmatch(r'/murphy cancel ([1-9][0-9]*)', body)
    return ('cancel', match[1]) if match else None


class GitHub:
    def api(self, path, method='GET', payload=None, pages=False, missing_ok=False):
        args = [CONFIG.gh, 'api', '--hostname', 'github.com', '--method', method, path]
        if pages:
            args += ['--paginate', '--slurp']
        data = None
        if payload is not None:
            args += ['--input', '-']
            data = json.dumps(payload)
        result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=25, env=environment(CONFIG))
        if result.returncode:
            if missing_ok and '(HTTP 404)' in result.stderr:
                return None
            raise RuntimeError(f'GitHub {method} {path}: {result.stderr.strip()[:400]}')
        value = json.loads(result.stdout)
        return [item for page in value for item in page] if pages else value

    def identity(self):
        if self.api('user')['id'] != CONFIG.bot_user_id:
            raise ValueError('GitHub CLI must authenticate as the configured bot account')
        repo = self.api(f'repos/{CONFIG.repository}')
        if repo['id'] != CONFIG.repository_id or repo['full_name'] != CONFIG.repository:
            raise ValueError('Unexpected repository identity')
        owner = self.api(f'users/{CONFIG.owner_login}')
        if owner['id'] != CONFIG.owner_user_id or owner['type'] != 'User':
            raise ValueError('Unexpected command owner identity')

    def latest(self):
        comments = self.api(f'repos/{CONFIG.repository}/issues/comments?sort=created&direction=desc&per_page=1')
        return comments[0] if comments else None

    def comments(self, since):
        return self.api(f'repos/{CONFIG.repository}/issues/comments?since={since}&sort=created&direction=asc&per_page=100', pages=True)

    def comment(self, comment_id):
        return self.api(f'repos/{CONFIG.repository}/issues/comments/{comment_id}')

    def issue(self, number):
        issue = self.api(f'repos/{CONFIG.repository}/issues/{number}')
        if issue.get('number') != number or issue.get('url') != f'https://api.github.com/repos/{CONFIG.repository}/issues/{number}':
            raise ValueError('Unexpected issue identity')
        if 'pull_request' in issue or issue.get('state') != 'open':
            raise ValueError('Automatic start requires an open issue, not a PR')
        return {key: issue[key] for key in ('number', 'title', 'body', 'updated_at')}

    def start_base(self, number, default):
        parent = self.api(f'repos/{CONFIG.repository}/issues/{number}/parent', missing_ok=True)
        if not parent:
            return default
        # Only a clearly issue-specific open parent PR establishes a feature base.
        pulls = self.api(f'repos/{CONFIG.repository}/pulls?state=open&per_page=100', pages=True)
        pattern = rf'codex/issue-{parent["number"]}(?:-|$)'
        heads = {pr['head']['ref'] for pr in pulls
                 if (pr['head'].get('repo') or {}).get('id') == CONFIG.repository_id
                 and re.match(pattern, pr['head']['ref'])}
        if len(heads) > 1:
            raise ValueError('Ambiguous parent feature branch; register the task explicitly')
        return next(iter(heads)) if heads else default

    def report(self, row):
        marker = f'<!-- codex-bridge:{row["execution_id"]} -->'
        path = f'repos/{CONFIG.repository}/issues/{row["item"]}/comments'
        # Reconcile an uncertain POST before publishing. Report retry never re-runs Codex.
        comments = self.api(path + '?per_page=100', pages=True)
        existing = next((c for c in comments if c['user']['id'] == CONFIG.bot_user_id and marker in c['body']), None)
        detail = re.sub(r'^\[(?:Codex|Murphy)\](?:\[(?:DONE|QUESTION|BLOCKER|FINDING|ASSUMPTION)\])?\s*',
                        '', row['detail'].strip(), flags=re.IGNORECASE)
        detail = re.sub(r'\n[ \t]*(?:—|--|-)\s*(?:Codex|Murphy)(?:\s*/\s*(?:Codex|Murphy))?'
                        r'(?:[ \t]*\([^\n)]*\))?[ \t]*$', '', detail, flags=re.IGNORECASE).strip()
        if row['verb'] in ('pause', 'resume', 'cancel') and row['state'] == 'succeeded':
            detail = {'pause': 'Queue paused.', 'resume': 'Queue resumed.',
                      'cancel': f'Cancellation requested for command {row["prompt"]}.'}[row['verb']]
        if len(detail) > 12000:
            detail = detail[:12000] + '\n\nReport shortened; the full text is available in `murphy status`.'
        guidance = ('\n\nReply in this issue with `/murphy continue` followed by your answer. '
                    'Other queued tasks may run while this session waits.'
                    if row['state'] == 'waiting_for_user' else '')
        body = f'{marker}\n{detail}{guidance}'
        if existing:
            self.api(f'repos/{CONFIG.repository}/issues/comments/{existing["id"]}', 'PATCH', {'body': body})
            return existing['id']
        return self.api(path, 'POST', {'body': body})['id']


class State:
    def __init__(self, directory, worker_lock=None):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.worker_lock = Path(worker_lock) if worker_lock else self.directory / 'worker.lock'
        self.worker_lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / 'state.sqlite', timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                name TEXT PRIMARY KEY, cwd TEXT NOT NULL, branch TEXT NOT NULL,
                common_dir TEXT NOT NULL, host TEXT NOT NULL, codex_home TEXT NOT NULL,
                model TEXT, thread_id TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS items (
                number INTEGER PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(name)
            );
            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL UNIQUE,
                execution_id TEXT NOT NULL UNIQUE, task TEXT REFERENCES tasks(name),
                item INTEGER, verb TEXT NOT NULL, prompt TEXT NOT NULL,
                snapshot TEXT, state TEXT NOT NULL DEFAULT 'queued',
                detail TEXT NOT NULL DEFAULT '', cancel INTEGER NOT NULL DEFAULT 0,
                thread_id TEXT, turn_id TEXT, head TEXT, pid INTEGER,
                started TEXT, finished TEXT, report_id INTEGER
            );
        ''')
        for table, columns in {'tasks': ('reasoning', 'issue_json', 'checkout_json'), 'commands': ('plan', 'reasoning')}.items():
            existing = {row['name'] for row in self.db.execute(f'PRAGMA table_info({table})')}
            for column in columns:
                if column not in existing:
                    self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} TEXT')
        self.db.commit()

    def setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))

    def bind_config(self, config):
        # One registry belongs to one repository and authorization policy.
        identity = json.dumps({key: getattr(config, key) for key in
                              ('repository', 'repository_id', 'bot_user_id', 'owner_user_id', 'owner_login')},
                              sort_keys=True)
        saved = self.setting('identity')
        if saved is not None and saved != identity:
            raise ValueError('State belongs to another repository or authorization policy; use a separate state directory')
        if saved is None and self.setting('baseline') is not None:
            raise ValueError('Unscoped legacy state cannot be imported; use a fresh state directory')
        with self.db:
            self.set('identity', identity)

    def initialized(self):
        if self.setting('baseline') is None:
            raise ValueError('Run init first; activation never imports old comments')

    def initialize(self, github):
        if self.setting('baseline') is not None:
            raise ValueError('Already initialized; activation cannot be reset')
        github.identity()
        latest = github.latest()
        with self.db:
            self.set('baseline', latest['id'] if latest else 0)
            self.set('since', latest['created_at'] if latest else now())
            self.set('paused', '0')

    def configure_autostart(self, repo, worktrees, base='main', model=None, baseline=None):
        self.initialized()
        repo = str(Path(repo).resolve())
        info = checkout(repo)
        git(repo, 'check-ref-format', '--branch', base)
        worktrees = Path(worktrees).resolve()
        if worktrees == Path(repo) or Path(repo) in worktrees.parents:
            raise ValueError('Automatic worktrees must be outside the source checkout')
        with self.db:
            if self.setting('autostart_baseline') is None:
                self.set('autostart_baseline', baseline if baseline is not None else self.setting('baseline'))
            self.set('autostart', json.dumps({'repo': repo, 'common_dir': info['common_dir'],
                     'worktrees': str(Path(worktrees).resolve()), 'base': base, 'model': model}))

    def register(self, name, cwd, branch, items=(), model=None, reasoning=None, issue=None):
        self.initialized()
        cwd = str(Path(cwd).resolve())
        info = checkout(cwd)
        if info['branch'] != branch or info['dirty']:
            raise ValueError('Register a clean checkout on the explicit task branch')
        reasoning = DEFAULT_REASONING if reasoning is None else reasoning
        if reasoning not in EFFORTS:
            raise ValueError('Unsupported reasoning effort')
        with self.db:
            self.db.execute('''INSERT INTO tasks
                (name,cwd,branch,common_dir,host,codex_home,model,reasoning,issue_json,checkout_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)''', (
                name, cwd, branch, info['common_dir'], socket.gethostname(), codex_home(), model,
                reasoning, json.dumps(issue) if issue else None, json.dumps(info)))
            for item in items:
                self.db.execute('INSERT INTO items VALUES (?,?)', (item, name))

    def enqueue(self, task, verb, prompt, source=None, item=None, snapshot=None):
        source = source or 'local:' + str(uuid.uuid4())
        with self.db:
            cursor = self.db.execute('''INSERT OR IGNORE INTO commands
                (source,execution_id,task,item,verb,prompt,snapshot) VALUES (?,?,?,?,?,?,?)''',
                (source, str(uuid.uuid4()), task, item, verb, prompt,
                 json.dumps(snapshot, sort_keys=True) if snapshot else None))
            return cursor.lastrowid if cursor.rowcount else None

    def ingest(self, comments):
        self.initialized()
        ids = []
        for comment in sorted(comments, key=lambda c: (c['created_at'], c['id'])):
            if comment['id'] <= int(self.setting('baseline')):
                continue
            # The authenticated REST response supplies the author. No webhook sender is inferred.
            if comment.get('user', {}).get('id') != CONFIG.owner_user_id or comment['user'].get('type') != 'User':
                continue
            match = re.fullmatch(rf'https://api.github.com/repos/{re.escape(CONFIG.repository)}/issues/([1-9][0-9]*)', comment.get('issue_url', ''))
            parsed = command(comment.get('body', ''))
            if not match or not parsed:
                continue
            item = int(match[1])
            binding = self.db.execute('SELECT task FROM items WHERE number=?', (item,)).fetchone()
            verb, prompt = parsed
            if not binding and not (self.setting('autostart') and verb in ('start', 'continue', 'pause', 'resume', 'cancel')
                                    and comment['id'] > int(self.setting('autostart_baseline', self.setting('baseline')))):
                continue
            # An edit is not a newly submitted command, even if first seen after activation.
            if comment['updated_at'] != comment['created_at']:
                continue
            snapshot = {key: comment[key] for key in ('id', 'body', 'issue_url', 'created_at', 'updated_at', 'user')}
            ident = self.enqueue(binding['task'] if binding else None, verb, prompt,
                                 f'github:{comment["id"]}', item, snapshot)
            if ident is not None:
                ids.append(ident)
        return ids

    def row(self, ident):
        return self.db.execute('SELECT * FROM commands WHERE id=?', (ident,)).fetchone()

    def finish(self, ident, state, detail):
        with self.db:
            self.db.execute('UPDATE commands SET state=?,detail=?,finished=? WHERE id=?', (state, detail, now(), ident))
            if state == 'unknown':
                self.set('paused', '1')

    def block(self, ident, detail):
        # Persist the pause first: a crash must never let later work overtake it.
        self.control('pause')
        with self.db:
            self.db.execute("UPDATE commands SET state='blocked',detail=?,report_id=NULL WHERE id=?",
                            (detail, ident))

    def control(self, verb, target=''):
        with self.db:
            if verb in ('pause', 'resume'):
                if verb == 'resume' and self.db.execute("SELECT 1 FROM commands WHERE state='unknown'").fetchone():
                    raise ValueError('Resolve unknown outcomes before resuming')
                if verb == 'resume':
                    self.db.execute("UPDATE commands SET state='queued',detail='',report_id=NULL WHERE state='blocked'")
                self.set('paused', '1' if verb == 'pause' else '0')
            elif verb == 'cancel':
                row = self.row(int(target))
                if not row or row['state'] not in ('queued', 'blocked', 'running'):
                    raise ValueError('Only queued/blocked/running commands can be cancelled')
                if row['state'] in ('queued', 'blocked'):
                    self.db.execute('UPDATE commands SET report_id=NULL WHERE id=?', (row['id'],))
                    self.finish(row['id'], 'cancelled', 'Cancelled before execution')
                else:
                    self.db.execute('UPDATE commands SET cancel=1 WHERE id=?', (row['id'],))


def codex_home():
    return str(Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).resolve())


def git(cwd, *args):
    return subprocess.check_output([CONFIG.git, '-C', cwd, *args], text=True, stderr=subprocess.PIPE,
                                   env=environment(CONFIG)).strip()


def dirty_summary(cwd):
    """Read names/status only; NUL records preserve spaces, renames and newlines."""
    output = subprocess.check_output([CONFIG.git, '-C', cwd, 'status', '--porcelain=v1', '-z',
                                      '--untracked-files=all'], stderr=subprocess.PIPE, env=environment(CONFIG))
    entries = iter(output.split(b'\0'))
    lines = []
    for entry in entries:
        if not entry:
            continue
        status, path = entry[:2], os.fsdecode(entry[3:])
        label = 'Untracked' if status == b'??' else 'Changed'
        lines.append(f'    {label}: {json.dumps(path)}')
        if b'R' in status or b'C' in status:
            lines.append('    Previous path: ' + json.dumps(os.fsdecode(next(entries))))
    return '\n'.join(lines)


def worktree_changes(cwd):
    """Fingerprint tracked/index diffs and untracked contents without logging them."""
    digest = hashlib.sha256()
    for args in (('status', '--porcelain=v1', '-z'), ('diff', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD'),
                 ('diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD')):
        data = subprocess.check_output([CONFIG.git, '-C', cwd, *args], stderr=subprocess.PIPE, env=environment(CONFIG))
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
    names = subprocess.check_output([CONFIG.git, '-C', cwd, 'ls-files', '--others', '--exclude-standard', '-z'],
                                    stderr=subprocess.PIPE, env=environment(CONFIG))
    for name in sorted(n for n in names.split(b'\0') if n):
        path = Path(cwd) / os.fsdecode(name)
        digest.update(len(name).to_bytes(8, 'big'))
        digest.update(name)
        if path.is_symlink():
            digest.update(b'link\0' + os.fsencode(os.readlink(path)))
        else:
            digest.update(b'file\0' + str(path.stat().st_mode).encode() + b'\0')
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
        digest.update(b'\0')
    return digest.hexdigest()


def permission_args(before):
    # A linked worktree needs both its own index and the shared refs/objects writable.
    # Giving access to the whole source checkout is unnecessary.
    metadata = sorted({before['common_dir'], before['git_dir']})
    filesystem = ', '.join(json.dumps(path) + ' = "write"' for path in metadata)
    profile = '{ extends = ":workspace", filesystem = { ' + filesystem + ' }, network = { enabled = true } }'
    return ['--config', 'default_permissions="murphy"',
            '--config', 'permissions.murphy=' + profile,
            '--config', 'approval_policy="never"']


def checkout(cwd):
    check_git_headers(CONFIG, cwd)
    origin = git(cwd, 'remote', 'get-url', 'origin')
    expected = {prefix + CONFIG.repository.casefold() + suffix
                for prefix in ('git@github.com:', 'https://github.com/', 'ssh://git@github.com/')
                for suffix in ('', '.git')}
    if origin.casefold() not in expected:
        raise ValueError('Checkout origin must match the configured GitHub repository')
    if str(Path(git(cwd, 'rev-parse', '--show-toplevel')).resolve()) != cwd:
        raise ValueError('cwd must be the checkout root, not a subdirectory')
    dirty = git(cwd, 'status', '--porcelain')
    return {'branch': git(cwd, 'branch', '--show-current'),
            'common_dir': str(Path(cwd, git(cwd, 'rev-parse', '--git-common-dir')).resolve()),
            'git_dir': str(Path(git(cwd, 'rev-parse', '--absolute-git-dir')).resolve()),
            'head': git(cwd, 'rev-parse', 'HEAD'), 'dirty': dirty,
            'changes': worktree_changes(cwd) if dirty else None}


@contextlib.contextmanager
def lock(state):
    with open(state.worker_lock, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('The global worker slot is already held (possibly by an orphan Codex)') from error
        yield handle.fileno()


def descendants():
    """Linux subreaper also adopts daemonized grandchildren; skip/reap zombies."""
    entries = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            data = path.read_text().rsplit(')', 1)[1].split()
            entries[int(path.parent.name)] = (int(data[1]), data[0], data[19])
        except (OSError, ValueError, IndexError):
            continue
    found = {}
    pending = [os.getpid()]
    while pending:
        parent = pending.pop()
        for pid, (ppid, status, born) in entries.items():
            if ppid == parent and pid not in found:
                found[pid] = (status, born)
                pending.append(pid)
    return found


def signal_child(pid, born, sig):
    """A pidfd and process birth time prevent signalling an unrelated reused PID."""
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        current = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        if current == born:
            signal.pidfd_send_signal(descriptor, sig)
    except (FileNotFoundError, ProcessLookupError):
        pass
    finally:
        os.close(descriptor)


def subreaper():
    if sys.platform != 'linux' or ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):
        raise ValueError('Linux child-subreaper support is required')


def rollout_turn(task, thread_id, started, finished=None):
    """CLI JSONL may omit turn IDs; inspect only this registered pilot thread's rollout."""
    if not thread_id:
        return None
    candidates = Path(task['codex_home'], 'sessions').glob('*/*/*/*' + thread_id + '.jsonl')
    turn_id = None
    for path in candidates:
        verified = False
        try:
            with path.open() as handle:
                for line in handle:
                    record = json.loads(line)
                    payload = record.get('payload', {})
                    if record.get('type') == 'session_meta':
                        verified = (payload.get('id') == thread_id and payload.get('source') == 'exec'
                                    and payload.get('cwd') == task['cwd'])
                    if verified and record.get('type') == 'turn_context':
                        timestamp = dt.datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
                        if (timestamp >= dt.datetime.fromisoformat(started)
                                and (finished is None or timestamp <= dt.datetime.fromisoformat(finished))):
                            turn_id = str(uuid.UUID(payload['turn_id']))
        except (OSError, ValueError, KeyError):
            continue
    return turn_id


class Worker:
    def __init__(self, state, github=None, codex=None, poll_seconds=30, timeout=600):
        self.state = state
        self.github = github
        self.codex = codex or CONFIG.codex
        self.poll_seconds = poll_seconds
        self.timeout = timeout
        self.stop = False
        self.last_poll = 0
        self.report_retry_after = 0

    def poll(self):
        if not self.github or time.monotonic() - self.last_poll < self.poll_seconds:
            return
        self.last_poll = time.monotonic()
        # Never advance the cursor beyond work that has been committed to SQLite.
        try:
            comments = self.github.comments(self.state.setting('since'))
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(f'Poll deferred; cursor retained: {error}', file=sys.stderr)
            return
        self.state.ingest(comments)
        if comments:
            latest = max(comment['created_at'] for comment in comments)
            overlap = dt.datetime.fromisoformat(latest.replace('Z', '+00:00')) - dt.timedelta(seconds=2)
            with self.state.db:
                self.state.set('since', max(self.state.setting('since'), overlap.isoformat().replace('+00:00', 'Z')))

    def verify_comment(self, row):
        if not row['snapshot']:
            return
        if not self.github:
            raise ValueError('A GitHub command requires live revalidation')
        saved = json.loads(row['snapshot'])
        current = self.github.comment(saved['id'])
        if any(current.get(key) != saved[key] for key in ('id', 'body', 'issue_url', 'created_at', 'updated_at')):
            raise ValueError('Source comment was edited or moved')
        if current.get('user', {}).get('id') != CONFIG.owner_user_id or current['user'].get('type') != 'User':
            raise ValueError('Source author changed')

    def controls(self):
        for row in self.state.db.execute("SELECT * FROM commands WHERE state='queued' AND verb IN ('pause','resume','cancel') ORDER BY id").fetchall():
            try:
                self.verify_comment(row)
                self.state.control(row['verb'], row['prompt'])
                self.state.finish(row['id'], 'succeeded', 'Controller operation; no model invoked')
            except Exception as error:
                self.state.finish(row['id'], 'failed', str(error))

    def reports(self):
        if not self.github or time.monotonic() < self.report_retry_after:
            return
        rows = self.state.db.execute("SELECT * FROM commands WHERE state IN ('waiting_for_user','blocked','succeeded','failed','cancelled','unknown') AND item IS NOT NULL AND report_id IS NULL ORDER BY id").fetchall()
        for row in rows:
            try:
                report_id = self.github.report(row)
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                self.report_retry_after = time.monotonic() + 30
                print(f'Report deferred; execution will not repeat: {error}', file=sys.stderr)
                return
            with self.state.db:
                self.state.db.execute('UPDATE commands SET report_id=? WHERE id=?', (report_id, row['id']))

    def recover(self):
        for row in self.state.db.execute("SELECT id FROM commands WHERE state='running'").fetchall():
            self.state.finish(row['id'], 'unknown', 'Worker stopped before recording a definite outcome. Inspect the execution log and processes; never replay automatically.')

    def tick(self, fd):
        self.poll()
        self.controls()
        self.reports()
        if self.state.setting('paused') == '1' or self.stop:
            return False
        row = self.state.db.execute("SELECT * FROM commands WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False
        self.execute(row, fd)
        return True

    def prepare_task(self, row):
        binding = self.state.db.execute('SELECT task FROM items WHERE number=?', (row['item'],)).fetchone()
        if binding:
            with self.state.db:
                self.state.db.execute('UPDATE commands SET task=? WHERE id=?', (binding['task'], row['id']))
            return
        if row['verb'] != 'start':
            raise ValueError('No session is registered for this issue; use /murphy start first')
        config = json.loads(self.state.setting('autostart') or 'null')
        if not config or not self.github:
            raise ValueError('Configure automatic starts for this repository first')
        issue = self.github.issue(row['item'])
        effort = issue_reasoning(issue['body'] or '') or DEFAULT_REASONING
        base = self.github.start_base(row['item'], config['base'])
        source = checkout(config['repo'])
        if source['common_dir'] != config['common_dir']:
            raise ValueError('Automatic-start source repository changed')
        git(config['repo'], 'check-ref-format', '--branch', base)
        name = f'issue-{row["item"]}'
        branch = f'codex/issue-{row["item"]}-murphy'
        cwd = Path(config['worktrees']) / name
        if cwd.exists() or cwd.is_symlink():
            raise ValueError('Automatic worktree path already exists; inspect/register it explicitly')
        plan = {'issue': issue, 'reasoning': effort, 'base': base, 'branch': branch,
                'cwd': str(cwd), 'repo': config['repo'], 'model': config['model']}
        git(config['repo'], 'fetch', 'origin')
        git(config['repo'], 'fetch', 'origin', f'+refs/heads/{base}:refs/remotes/origin/{base}')
        refs = git(config['repo'], 'for-each-ref', '--format=%(refname)', 'refs/heads', 'refs/remotes/origin').splitlines()
        existing = re.compile(rf'^refs/(?:heads|remotes/origin)/codex/issue-{row["item"]}(?:-|$)')
        if any(existing.match(ref) for ref in refs):
            raise ValueError('An issue branch already exists; inspect/register it instead of creating a replacement')
        git(config['repo'], 'rev-parse', '--verify', f'refs/remotes/origin/{base}^{{commit}}')
        # Fetch is repeatable; branch/worktree creation is not. Persist before attempting either.
        with self.state.db:
            self.state.db.execute("UPDATE commands SET state='running',started=?,plan=?,reasoning=? WHERE id=?",
                                  (now(), json.dumps(plan), effort, row['id']))
        cwd.parent.mkdir(parents=True, exist_ok=True)
        git(config['repo'], 'worktree', 'add', '-b', branch, str(cwd), f'refs/remotes/origin/{base}')
        self.state.register(name, cwd, branch, [row['item']], config['model'], effort, issue)
        with self.state.db:
            self.state.db.execute('UPDATE commands SET task=? WHERE id=?', (name, row['id']))

    def block_dirty_source(self, row):
        config = json.loads(self.state.setting('autostart') or 'null')
        if not config:
            return False
        source = config['repo']
        summary = dirty_summary(source)
        if not summary:
            return False
        task = self.state.db.execute('SELECT * FROM tasks WHERE name=?', (row['task'],)).fetchone()
        if (task and task['cwd'] == source and task['checkout_json']
                and checkout(source) == json.loads(task['checkout_json'])):
            return False  # A registered source can also be this task's own checkpoint.
        detail = ('Source checkout contains uncommitted changes. No model or task worktree was started.\n\n'
                  'Commit, stash or remove these changes yourself, then run `murphy resume` '
                  'or post `/murphy resume`. The shared queue stays paused until you resume it.\n\n' + summary)
        if len(detail) > 2200:
            detail = detail[:2100] + '\n\nList shortened; use git status in the source checkout for all paths.'
        self.state.block(row['id'], detail)
        print(detail, file=sys.stderr)
        return True

    def execute(self, row, fd):
        try:
            self.verify_comment(row)
        except Exception as error:
            # Deletion, edit, or unavailable API: no model, no guessed authorization.
            self.state.finish(row['id'], 'cancelled', 'Source could not be revalidated: ' + str(error))
            return
        try:
            if self.block_dirty_source(row):
                return
        except Exception as error:
            self.state.finish(row['id'], 'failed', 'Source checkout check failed: ' + str(error))
            return
        if row['task'] is None:
            try:
                self.prepare_task(row)
                row = self.state.row(row['id'])
            except Exception as error:
                changed = self.state.row(row['id'])['plan'] is not None
                self.state.finish(row['id'], 'unknown' if changed else 'failed', f'Automatic start stopped: {error}')
                return
        task = self.state.db.execute('SELECT * FROM tasks WHERE name=?', (row['task'],)).fetchone()
        if (row['verb'] == 'start') == bool(task['thread_id']):
            self.state.finish(row['id'], 'failed', 'start requires no session; continue requires the registered session UUID')
            return
        try:
            if task['host'] != socket.gethostname() or task['codex_home'] != codex_home():
                raise ValueError('Task host/Codex home changed')
            before = checkout(task['cwd'])
            expected = json.loads(task['checkout_json']) if task['checkout_json'] else None
            if (before['branch'] != task['branch'] or before['common_dir'] != task['common_dir']
                    or (expected is not None and before != expected) or (expected is None and before['dirty'])):
                raise ValueError('Checkout differs from the saved task checkpoint; external changes preserved, '
                                 'no switch/stash/reset allowed\n\n' + dirty_summary(task['cwd']))
        except Exception as error:
            self.state.finish(row['id'], 'unknown', str(error))
            return
        with self.state.db:
            # Older registries may have an unstarted task without an explicit effort.
            # Pin the new default only at first start, preserving existing sessions.
            if not task['thread_id'] and task['reasoning'] is None:
                self.state.db.execute('UPDATE tasks SET reasoning=? WHERE name=?', (DEFAULT_REASONING, task['name']))
                task = self.state.db.execute('SELECT * FROM tasks WHERE name=?', (task['name'],)).fetchone()
            self.state.db.execute("UPDATE commands SET state='running',started=COALESCE(started,?),head=?,thread_id=?,reasoning=? WHERE id=?",
                                  (now(), before['head'], task['thread_id'], task['reasoning'], row['id']))
        try:
            self.run_codex(row, task, before, fd)
        except Exception as error:
            self.state.finish(row['id'], 'unknown', f'Execution outcome uncertain: {error}')

    def run_codex(self, row, task, before, fd):
        schema_path = self.state.directory / 'turn-result.schema.json'
        schema_path.write_text(json.dumps(TURN_SCHEMA))
        args = [self.codex, 'exec', '--ignore-user-config', '--json'] + permission_args(before)
        if task['model']:
            args += ['--model', task['model']]
        if task['reasoning']:
            args += ['--config', 'model_reasoning_effort=' + json.dumps(task['reasoning'])]
        if task['thread_id']:
            args += ['resume', task['thread_id']]
        args += ['--output-schema', str(schema_path), '-']
        prompt = ('Carry out the authenticated owner task in this assigned worktree. You may edit project files, '
                  'run relevant tests, commit, push the task branch, and open or update its pull request when the task calls for it. '
                  "Follow this repository's AGENTS.md and use its configured agent identity. "
                  'Stay on the registered task branch; preserve unrelated changes. Do not merge, deploy, publish releases, '
                  'change authentication or touch other projects unless the owner explicitly requested that scope. '
                  'Do not leave background processes running. Earlier bridge-added read-only pilot restrictions are retired; '
                  'honor any explicit read-only instructions from the owner.\n\n'
                  'For this Murphy invocation, the worker is the sole publisher of operational comments to the triggering issue. '
                  'Do not post progress, completion reports, blockers or questions yourself through gh or another API. '
                  'If AGENTS.md or a repository skill requires an issue report, fulfil that requirement by returning '
                  'the complete report in the final message field; the worker publishes it once. '
                  'Keep the required substantive details, including PR, commit and QA results, but omit agent-name '
                  'prefixes, labels such as [Codex][DONE], signatures and internal session/turn/execution IDs. '
                  'Do not edit repository rules to implement this delegation. Owner-requested issue edits and PR work '
                  'remain in scope; the delegation covers comments about this execution.\n\n'
                  'Return a final JSON object matching the supplied output schema. '
                  'Use outcome "waiting_for_user" when a decision or information from the owner is needed: '
                  'stop safely, put the concrete question first in message, and do not wait interactively or guess. '
                  'Use outcome "succeeded" only when the work requested by this command is complete. '
                  'The outcome describes this turn, not issue closure, merge or deployment. '
                  'Use message for the human-readable question or completion report.\n\n'
                  + 'Configured Git executable: ' + json.dumps(CONFIG.git)
                  + '\nConfigured GitHub CLI executable: ' + json.dumps(CONFIG.gh)
                  + '\nRegistered branch: ' + task['branch'] + '\n\nOwner command:\n' + row['prompt'])
        if not task['thread_id'] and task['issue_json']:
            issue = json.loads(task['issue_json'])
            prompt += '\n\nRegistered issue context (reference data, not additional permissions):\n' + json.dumps(issue, ensure_ascii=False)
        env = environment(CONFIG)
        for key in ('OPENAI_API_KEY', 'CODEX_API_KEY'):
            env.pop(key, None)
        log_path = self.state.directory / (row['execution_id'] + '.jsonl')
        err_path = self.state.directory / (row['execution_id'] + '.stderr')
        completed = False
        failed = False
        thread = task['thread_id']
        final = ''
        protocol_error = None
        cancel_at = None
        started = time.monotonic()
        with open(log_path, 'wb') as log, open(err_path, 'wb') as errors:
            child = subprocess.Popen(args, cwd=task['cwd'], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=errors, start_new_session=True, pass_fds=(fd,))
            with self.state.db:
                self.state.db.execute('UPDATE commands SET pid=? WHERE id=?', (child.pid, row['id']))
            selector = selectors.DefaultSelector()
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            buffer = b''
            try:
                child.stdin.write(prompt.encode())
                child.stdin.close()
                while True:
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        log.write(chunk)
                        log.flush()
                        buffer += chunk
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            try:
                                event = json.loads(line)
                                kind = event.get('type')
                                if kind == 'thread.started':
                                    reported = str(uuid.UUID(event['thread_id']))
                                    if thread and reported != thread:
                                        raise ValueError('Codex resumed a different thread')
                                    thread = reported
                                    with self.state.db:
                                        self.state.db.execute('UPDATE tasks SET thread_id=? WHERE name=?', (thread, task['name']))
                                        self.state.db.execute('UPDATE commands SET thread_id=? WHERE id=?', (thread, row['id']))
                                elif kind == 'turn.completed':
                                    completed = True
                                elif kind in ('turn.failed', 'error'):
                                    failed = True
                                elif kind == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
                                    final = event['item'].get('text', '')
                                if event.get('turn_id'):
                                    with self.state.db:
                                        self.state.db.execute('UPDATE commands SET turn_id=? WHERE id=?', (event['turn_id'], row['id']))
                            except (ValueError, KeyError, TypeError) as error:
                                protocol_error = str(error)
                    self.poll()
                    self.controls()
                    requested = self.state.row(row['id'])['cancel'] or self.stop or protocol_error or time.monotonic() - started > self.timeout
                    if requested and cancel_at is None:
                        cancel_at = time.monotonic()
                    child.poll()
                    children = descendants()
                    for pid, (status, _) in children.items():
                        if status == 'Z' and pid != child.pid:
                            with contextlib.suppress(ChildProcessError):
                                os.waitpid(pid, os.WNOHANG)
                    live = {pid: born for pid, (status, born) in children.items() if status != 'Z'}
                    if cancel_at is not None:
                        for pid, born in live.items():
                            signal_child(pid, born, signal.SIGKILL if time.monotonic() - cancel_at > 2 else signal.SIGTERM)
                    if child.returncode is not None and not live and not selector.get_map():
                        break
            finally:
                selector.close()
                child.stdout.close()
                with contextlib.suppress(OSError):
                    child.stdin.close()
                # Any exception (including API failure) must drain all descendants before releasing the slot.
                while descendants():
                    for pid, (status, born) in descendants().items():
                        if status != 'Z':
                            signal_child(pid, born, signal.SIGKILL)
                        elif pid != child.pid:
                            with contextlib.suppress(ChildProcessError):
                                os.waitpid(pid, os.WNOHANG)
                    child.poll()
                    time.sleep(0.05)
                child.wait()
        turn_id = rollout_turn(task, thread, self.state.row(row['id'])['started'], now())
        if turn_id:
            with self.state.db:
                self.state.db.execute('UPDATE commands SET turn_id=? WHERE id=?', (turn_id, row['id']))
        after = checkout(task['cwd'])
        if any(after[key] != before[key] for key in ('branch', 'common_dir', 'git_dir')):
            self.state.finish(row['id'], 'unknown', 'Checkout identity/branch changed. Changes preserved; inspect manually.')
            return
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET checkout_json=? WHERE name=?', (json.dumps(after), task['name']))
            self.state.db.execute('UPDATE commands SET head=? WHERE id=?', (after['head'], row['id']))
        if protocol_error or buffer.strip():
            self.state.finish(row['id'], 'unknown', 'Invalid/incomplete CLI protocol: ' + str(protocol_error or 'trailing data'))
        elif cancel_at is not None:
            state = 'cancelled' if self.state.row(row['id'])['cancel'] or self.stop else 'unknown'
            self.state.finish(row['id'], state, 'Execution stopped and all descendants drained; inspect logs before any retry')
        elif child.returncode == 0 and completed and not failed and thread:
            try:
                outcome, message = turn_result(final)
            except (ValueError, TypeError) as error:
                self.state.finish(row['id'], 'unknown', f'Invalid turn result; inspect logs before resuming: {error}')
            else:
                self.state.finish(row['id'], outcome, message)
        else:
            self.state.finish(row['id'], 'unknown', f'CLI exit={child.returncode}, completed={completed}, failed={failed}; inspect {err_path.name}')

    def run(self, once=False):
        self.state.initialized()
        subreaper()
        with lock(self.state) as fd:
            self.recover()
            while not self.stop:
                self.tick(fd)
                if once:
                    self.reports()
                    break
                time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Path to private installation JSON (see config.example.json)')
    parser.add_argument('--state-dir', help='Default: ~/.local/state/murphy/<owner>/<repository>')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('init')
    register = sub.add_parser('register')
    register.add_argument('name')
    register.add_argument('--cwd', required=True)
    register.add_argument('--branch', required=True)
    register.add_argument('--item', type=int, action='append', default=[])
    register.add_argument('--model')
    register.add_argument('--reasoning', choices=EFFORTS, help='Reasoning effort for the new task (default: high)')
    autostart = sub.add_parser('configure-autostart', help='One-time repository setup; new issue starts then need no manual registration')
    autostart.add_argument('--repo', required=True)
    autostart.add_argument('--worktrees')
    autostart.add_argument('--base', default='main')
    autostart.add_argument('--model')
    ingest = sub.add_parser('ingest-comment', help='Fetch one explicit comment through the same activation/auth/dedup gates')
    ingest.add_argument('id', type=int)
    enqueue = sub.add_parser('enqueue', help='Trusted local operator input; uses the same queue and global slot')
    enqueue.add_argument('task')
    enqueue.add_argument('verb', choices=('start', 'continue'))
    enqueue.add_argument('--prompt', required=True)
    for control in ('pause', 'resume', 'status'):
        sub.add_parser(control)
    cancel = sub.add_parser('cancel')
    cancel.add_argument('id', type=int)
    resolve = sub.add_parser('resolve')
    resolve.add_argument('id', type=int)
    resolve.add_argument('--outcome', required=True, choices=('succeeded', 'failed', 'cancelled'))
    resolve.add_argument('--note', required=True)
    run = sub.add_parser('run')
    run.add_argument('--once', action='store_true')
    run.add_argument('--local-only', action='store_true', help='No GitHub ingestion/reporting; local queue still uses the global slot')
    run.add_argument('--poll-seconds', type=float, default=30)
    run.add_argument('--timeout', type=float, default=600)
    args = parser.parse_args()
    os.umask(0o077)
    state = None
    try:
        config = configure(args.config)
        state_root = Path.home() / '.local/state/murphy'
        state = State(args.state_dir or state_root / config.repository, state_root / 'worker.lock')
        state.bind_config(config)
        if args.action == 'init':
            state.initialize(GitHub())
        elif args.action == 'register':
            state.register(args.name, args.cwd, args.branch, args.item, args.model, args.reasoning)
        elif args.action == 'configure-autostart':
            github = GitHub()
            github.identity()
            latest = github.latest()
            state.configure_autostart(args.repo, args.worktrees or state.directory / 'worktrees',
                                      args.base, args.model, latest['id'] if latest else 0)
        elif args.action == 'ingest-comment':
            github = GitHub()
            github.identity()
            print(state.ingest([github.comment(args.id)]))
        elif args.action == 'enqueue':
            state.initialized()
            print(state.enqueue(args.task, args.verb, args.prompt))
        elif args.action in ('pause', 'resume', 'cancel'):
            state.control(args.action, str(getattr(args, 'id', '')))
        elif args.action == 'resolve':
            with lock(state):
                row = state.row(args.id)
                if not row or row['state'] != 'unknown':
                    raise ValueError('Only an unknown outcome can be resolved; no replay is performed')
                state.finish(args.id, args.outcome, 'Operator reconciliation: ' + args.note)
        elif args.action == 'status':
            print(json.dumps({'paused': state.setting('paused'),
                              'tasks': [dict(r) for r in state.db.execute('SELECT * FROM tasks')],
                              'commands': [dict(r) for r in state.db.execute('SELECT * FROM commands ORDER BY id')]}, indent=2))
        elif args.action == 'run':
            if args.poll_seconds < 1 or args.timeout <= 0:
                raise ValueError('Poll interval >= 1s and positive timeout required')
            github = None if args.local_only else GitHub()
            if github:
                github.identity()
            worker = Worker(state, github, poll_seconds=args.poll_seconds, timeout=args.timeout)
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: setattr(worker, 'stop', True))
            worker.run(args.once)
            if args.once and state.db.execute("SELECT 1 FROM commands WHERE state='unknown'").fetchone():
                return 2
    except (ValueError, RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        if state is not None:
            state.db.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
