"""Read-only worker snapshots and a small terminal view, using only the stdlib."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata


LABELS = {'queued': 'W KOLEJCE', 'running': 'PRACUJE', 'waiting_for_user': 'CZEKA NA CIEBIE',
          'succeeded': 'ZAKOŃCZONE', 'failed': 'BŁĄD', 'blocked': 'ZABLOKOWANE',
          'cancelled': 'ANULOWANE', 'unknown': 'WYNIK NIEZNANY'}
PENDING = ('queued', 'running', 'waiting_for_user', 'blocked', 'unknown')
ALWAYS_VISIBLE = ('queued', 'running', 'blocked', 'unknown')


def task_commands(connection):
    """Latest turn per session, plus every queued, active or unsafe command."""
    return connection.execute('''WITH latest AS (
        SELECT MAX(c.id) AS id FROM commands c LEFT JOIN items b ON b.number=c.item
        WHERE c.verb IN ('start','continue')
        GROUP BY CASE WHEN COALESCE(c.task,b.task) IS NOT NULL THEN 'task:' || COALESCE(c.task,b.task)
                      WHEN c.item IS NOT NULL THEN 'issue:' || c.item
                      ELSE 'command:' || c.id END
    ) SELECT c.id,c.item,c.task,c.state,c.started,c.finished,c.detail,c.execution_id,t.issue_json
        FROM commands c LEFT JOIN items b ON b.number=c.item
        LEFT JOIN tasks t ON t.name=COALESCE(c.task,b.task)
        WHERE c.verb IN ('start','continue') AND
        (c.id IN (SELECT id FROM latest) OR c.state IN ('queued','running','blocked','unknown'))
        ORDER BY c.id''').fetchall()


def issue_cache(value):
    try:
        cache = json.loads(value or '{}')
        return {key: item for key, item in cache.items()
                if isinstance(item, dict) and item.get('state') in ('OPEN', 'CLOSED', 'MERGED', 'DELETED', 'UNKNOWN')
                and isinstance(item.get('title', ''), str)
                and isinstance(item.get('checked'), (int, float))}
    except (ValueError, AttributeError, TypeError):
        return {}


def state_directory(directory=None):
    return Path(directory or Path.home() / '.local/state/murphy/manager').expanduser().resolve()


def process_birth(pid):
    try:
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] not in ('Z', 'X') else None
    except (OSError, ValueError, IndexError, TypeError):
        return None


@contextlib.contextmanager
def worker_record(directory, parallel):
    """Written only by the coordinator while holding its existing worker lock."""
    path = Path(directory) / 'worker.json'
    temporary = path.with_suffix('.tmp')
    record = {'pid': os.getpid(), 'born': process_birth(os.getpid()), 'parallel': parallel}
    try:
        temporary.write_text(json.dumps(record))
        temporary.replace(path)
        yield
    finally:
        path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def read_worker(directory):
    try:
        record = json.loads((directory / 'worker.json').read_text())
        if record['born'] and process_birth(record['pid']) == record['born']:
            return record
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


@contextlib.contextmanager
def database(path):
    # Unlike Manager.status(), a monitor must not create/migrate state or enroll commands.
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.2)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def tail(path, limit=65536):
    try:
        with path.open('rb') as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - limit))
            data = handle.read(limit)
        return data, path.stat().st_mtime
    except OSError:
        return b'', None


def activity(path):
    data, modified = tail(path)
    # Ignore the trailing partial record; stdout may be between writes.
    for line in reversed(data.split(b'\n')[:-1]):
        try:
            event = json.loads(line)
            item = event.get('item', {})
            if item.get('type') == 'command_execution':
                return {'text': 'Polecenie: ' + item.get('command', ''), 'updated': modified}
            if item.get('type') == 'agent_message':
                return {'text': item.get('text', ''), 'updated': modified}
        except (ValueError, TypeError, AttributeError):
            continue
    return {'text': '', 'updated': modified}


def snapshot(directory=None):
    """Plain data for terminal rendering; no GitHub, model or state writes."""
    directory = state_directory(directory)
    with database(directory / 'manager.sqlite') as manager:
        paused = manager.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
        projects = manager.execute('SELECT id,repository FROM projects ORDER BY id').fetchall()
        tickets = {(row['project'], row['command']): row['id'] for row in manager.execute('SELECT * FROM queue')}
    commands, hidden_closed = [], 0
    for project in projects:
        project_dir = directory / 'projects' / str(project['id'])
        with database(project_dir / 'state.sqlite') as state:
            saved = state.execute("SELECT value FROM settings WHERE key='monitor_issues'").fetchone()
            cache = issue_cache(saved[0] if saved else None)
            for row in task_commands(state):
                ident = tickets.get((project['id'], row['id']))
                if ident is None:  # Enrollment belongs to the coordinator, including after a crash.
                    continue
                issue = cache.get(str(row['item']), {})
                if issue.get('state') in ('CLOSED', 'MERGED', 'DELETED') and row['state'] not in ALWAYS_VISIBLE:
                    hidden_closed += 1
                    continue
                title = ''
                if row['issue_json']:
                    with contextlib.suppress(ValueError, AttributeError):
                        title = json.loads(row['issue_json']).get('title', '')
                title = issue.get('title') or title
                task = f'#{row["item"]}' if row['item'] else row['task'] or 'Zadanie lokalne'
                commands.append({'id': ident, 'repository': project['repository'],
                                 'task': task + (' ' + title if title else ''),
                                 'state': row['state'], 'started': row['started'], 'finished': row['finished'],
                                 'detail': row['detail'],
                                 'activity': activity(project_dir / (row['execution_id'] + '.jsonl'))
                                 if row['state'] == 'running' else None})
    pending = sorted((row for row in commands if row['state'] in PENDING),
                     key=lambda row: (row['state'] != 'running', row['id']))
    recent = sorted((row for row in commands if row['state'] not in PENDING),
                    key=lambda row: (row['finished'] or '', row['id']), reverse=True)[:5]
    return {'worker': read_worker(directory), 'paused': bool(paused and paused[0] == '1'),
            'commands': pending + recent, 'observed': time.time(), 'hidden_closed': hidden_closed}


def clean(value):
    return ''.join(char if char.isprintable() else ' ' for char in str(value))


def fit(value, width):
    text = clean(value)
    result, sizes, used = [], [], 0
    for char in text:
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if used + size > width:
            if not width:
                return ''
            while result and used > width - 1:
                result.pop()
                used -= sizes.pop()
            return ''.join(result) + '…' + ' ' * (width - used - 1)
        result.append(char)
        sizes.append(size)
        used += size
    return ''.join(result) + ' ' * (width - used)


def duration(started, finished, observed):
    if not started:
        return '—'
    end = dt.datetime.fromisoformat(finished).timestamp() if finished else observed
    seconds = max(0, int(end - dt.datetime.fromisoformat(started).timestamp()))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


def render(data, width=100, height=24, footer='Ctrl+C — zamknij podgląd'):
    if width < 1 or height < 1:
        return ''
    worker, rows, observed = data['worker'], data['commands'], data['observed']
    active = sum(row['state'] == 'running' for row in rows) if worker else 0
    queued = sum(row['state'] == 'queued' for row in rows)
    state = 'WYŁĄCZONY' if not worker else 'PAUZA' if data['paused'] else 'PRACUJE' if active else 'BEZCZYNNY'
    state = data.get('phase') or state
    slots = f'/{worker["parallel"]}' if worker else ''
    lines = [f'Murphy · {state} · aktywne: {active}{slots} · w kolejce: {queued}',
             'Odświeżono: ' + dt.datetime.fromtimestamp(observed).strftime('%H:%M:%S'), '']
    bottom = []
    if data.get('hidden_closed'):
        bottom.append(f'Ukryto zamknięte/usunięte w GitHub: {data["hidden_closed"]}')
    if data.get('diagnostic'):
        bottom.append('Worker: ' + data['diagnostic'])
    bottom.extend(['', footer])
    id_width = max(2, max((len(str(row['id'])) for row in rows), default=2))
    repo_width = min(28, max(10, max((len(row['repository']) for row in rows), default=10)), max(10, width // 4))
    status_width = max(6, max((len(LABELS.get(row['state'], row['state'])) for row in rows), default=6))
    status_width = max(status_width, len('BRAK WORKERA') if not worker and any(row['state'] == 'running' for row in rows) else 0)
    time_width = max(4, max((len(duration(row['started'], row['finished'], observed)) for row in rows), default=4))
    task_width = width - id_width - repo_width - status_width - time_width - 12
    wide = task_width >= 16
    widths = (id_width, repo_width, task_width, status_width, time_width)
    if wide and rows:
        lines.append(' │ '.join(fit(value, size) for value, size in zip(('ID', 'REPO', 'ZADANIE', 'STATUS', 'CZAS'), widths)))
        lines.append('─┼─'.join('─' * size for size in widths))
    for index, row in enumerate(rows):
        if len(lines) + (1 if wide else 2) > height - len(bottom) - 1:
            lines.append(f'… pozostałe zadania: {len(rows) - index}')
            break
        status = LABELS.get(row['state'], row['state'])
        elapsed = duration(row['started'], row['finished'], observed)
        if row['state'] == 'running' and not worker:
            status, elapsed = 'BRAK WORKERA', '—'
        if wide:
            values = (row['id'], row['repository'], row['task'], status, elapsed)
            lines.append(' │ '.join(fit(value, size) for value, size in zip(values, widths)))
        else:
            lines.extend([f'{row["id"]} · {status} · {elapsed}', f'  {row["repository"]} · {row["task"]}'])
        if row['activity'] and len(lines) < height - len(bottom) - 1:
            event = row['activity']
            age = f'{max(0, int(observed - event["updated"]))} s temu' if event['updated'] else 'brak zapisów'
            lines.append(f'  Log: {age}' + (' · ' + event['text'] if event['text'] else ''))
        elif row['state'] in ('waiting_for_user', 'blocked', 'unknown', 'failed') and row['detail'] and len(lines) < height - len(bottom) - 1:
            summary = next((line.strip() for line in row['detail'].splitlines() if line.strip()), '')
            lines.append('  ' + summary)
    if not rows:
        lines.append('Brak zadań. Oczekiwanie na polecenia.' if worker else 'Brak zadań. Worker nie jest uruchomiony.')
    lines = lines[:max(0, height - len(bottom))] + bottom
    return '\n'.join(fit(line, width).rstrip() for line in lines[-height:])


class Terminal:
    def __init__(self, footer):
        self.footer = footer
        self.tty = sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'
        self.previous = None
        self.last = None

    def __enter__(self):
        if self.tty:
            print('\x1b[?1049h\x1b[?25l', end='', flush=True)
        return self

    def draw(self, data):
        size = shutil.get_terminal_size((100, 24))
        text = render(data, size.columns, max(1, size.lines - 1), self.footer)
        # Pipes get state changes, without ANSI escapes or a new timer line every second.
        signature = [(row['id'], row['state'], row['activity']) for row in data['commands']]
        signature = (data['worker'], data['paused'], signature, data.get('diagnostic'), data.get('phase'), data.get('hidden_closed'))
        if self.tty or signature != self.previous:
            print(('\x1b[H\x1b[2J' if self.tty else '') + text, flush=True)
        self.previous, self.last = signature, data

    def __exit__(self, *_):
        if self.tty:
            print('\x1b[?25h\x1b[?1049l', end='', flush=True)
            if self.last:
                size = shutil.get_terminal_size((100, 24))
                print(render(self.last, size.columns, max(1, size.lines - 1), footer='').rstrip())


def watch(directory=None, once=False):
    def stop(*_):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        with Terminal('Ctrl+C — zamknij podgląd (worker pracuje dalej)') as terminal:
            while True:
                terminal.draw(snapshot(directory))
                if once or not terminal.tty:
                    return 0
                time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_with_monitor(directory, options):
    """Keep rendering independent of blocking worker API calls, without forking threads."""
    command = [sys.executable, '-m', 'murphy']
    if directory:
        command += ['--state-dir', directory]
    command += ['run', *options]
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                with Terminal('Ctrl+C — zatrzymaj Murphy’ego') as terminal:
                    sent_stop = False
                    while True:
                        result = process.poll()
                        if stopping and not sent_stop and process.poll() is None:
                            # Include startup/API helpers even before the coordinator installs handlers.
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(process.pid, signal.SIGTERM)
                            sent_stop = True
                        read_error = ''
                        try:
                            data = snapshot(directory)
                        except sqlite3.OperationalError as error:
                            data = {'worker': read_worker(state_directory(directory)), 'paused': False,
                                    'commands': [], 'observed': time.time()}
                            read_error = 'Nie można odczytać stanu: ' + str(error)
                        size = os.fstat(output.fileno()).st_size
                        diagnostic = os.pread(output.fileno(), min(size, 4096), max(0, size - 4096)).decode('utf-8', errors='replace')
                        data['diagnostic'] = read_error or (clean(diagnostic.splitlines()[-1]) if diagnostic.splitlines() else '')
                        if not data['worker'] and result is None:
                            data['phase'] = 'URUCHAMIANIE'
                        if sent_stop:
                            data['phase'] = 'ZATRZYMYWANIE' if result is None else None
                            data['diagnostic'] = 'Zatrzymywanie workera i jego procesów…' if result is None else 'Worker zatrzymany.'
                        terminal.draw(data)
                        if result is not None:
                            break
                        time.sleep(1)
            finally:
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    process.wait()
            # Preserve the original diagnostics after leaving the alternate screen.
            output.seek(0)
            while chunk := output.read(65536):
                sys.stderr.write(chunk.decode('utf-8', errors='replace'))
            return result if result >= 0 else 128 - result
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
