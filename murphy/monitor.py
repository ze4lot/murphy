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
    commands = []
    for project in projects:
        project_dir = directory / 'projects' / str(project['id'])
        with database(project_dir / 'state.sqlite') as state:
            rows = state.execute('''SELECT c.id,c.item,c.task,c.state,c.started,c.finished,c.detail,
                c.execution_id,t.issue_json FROM commands c LEFT JOIN tasks t ON t.name=c.task
                WHERE c.verb IN ('start','continue') AND (c.state IN (?,?,?,?,?) OR c.id IN (
                    SELECT id FROM commands WHERE verb IN ('start','continue')
                    AND state NOT IN (?,?,?,?,?) ORDER BY finished DESC,id DESC LIMIT 5))''', PENDING * 2)
            for row in rows:
                ident = tickets.get((project['id'], row['id']))
                if ident is None:  # Enrollment belongs to the coordinator, including after a crash.
                    continue
                title = ''
                if row['issue_json']:
                    with contextlib.suppress(ValueError, AttributeError):
                        title = json.loads(row['issue_json']).get('title', '')
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
            'commands': pending + recent, 'observed': time.time()}


def clean(value):
    return ''.join(char if char.isprintable() else ' ' for char in str(value))


def fit(value, width):
    text = clean(value)
    result, used = '', 0
    for char in text:
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if used + size > width:
            return fit(result, max(0, width - 1)).rstrip() + '…' if width else ''
        result += char
        used += size
    return result + ' ' * (width - used)


def duration(started, finished, observed):
    if not started:
        return '—'
    end = dt.datetime.fromisoformat(finished).timestamp() if finished else observed
    seconds = max(0, int(end - dt.datetime.fromisoformat(started).timestamp()))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


def render(data, width=100, height=24, footer='Ctrl+C — zamknij podgląd'):
    worker, rows, observed = data['worker'], data['commands'], data['observed']
    active = sum(row['state'] == 'running' for row in rows) if worker else 0
    queued = sum(row['state'] == 'queued' for row in rows)
    state = 'WYŁĄCZONY' if not worker else 'PAUZA' if data['paused'] else 'PRACUJE' if active else 'BEZCZYNNY'
    state = data.get('phase') or state
    slots = f'/{worker["parallel"]}' if worker else ''
    lines = [f'Murphy · {state} · aktywne: {active}{slots} · w kolejce: {queued}',
             'Odświeżono: ' + dt.datetime.fromtimestamp(observed).strftime('%H:%M:%S'), '']
    wide = width >= 80
    task_width = max(10, width - 57)
    if wide:
        lines.append(f'{"ID":<6} {"REPO":<22} {fit("ZADANIE", task_width)} {"STATUS":<17} CZAS')
    for index, row in enumerate(rows):
        if len(lines) >= height - 4:
            lines.append(f'… pozostałe zadania: {len(rows) - index}')
            break
        status = LABELS.get(row['state'], row['state'])
        elapsed = duration(row['started'], row['finished'], observed)
        if row['state'] == 'running' and not worker:
            status, elapsed = 'BRAK WORKERA', '—'
        if wide:
            lines.append(f'{row["id"]:<6} {fit(row["repository"], 22)} {fit(row["task"], task_width)} {status:<17} {elapsed}')
        else:
            lines.extend([f'{row["id"]} · {row["repository"]} · {status} · {elapsed}', '  ' + row['task']])
        if row['activity'] and len(lines) < height - 3:
            event = row['activity']
            age = f'{max(0, int(observed - event["updated"]))} s temu' if event['updated'] else 'brak zapisów'
            lines.append(f'  Log: {age}' + (' · ' + event['text'] if event['text'] else ''))
        elif row['state'] in ('waiting_for_user', 'blocked', 'unknown', 'failed') and row['detail'] and len(lines) < height - 3:
            lines.append('  ' + row['detail'])
    if not rows:
        lines.append('Brak zadań. Oczekiwanie na polecenia.' if worker else 'Brak zadań. Worker nie jest uruchomiony.')
    if data.get('diagnostic'):
        lines.append('Worker: ' + data['diagnostic'])
    lines.extend(['', footer])
    return '\n'.join(fit(line, width).rstrip() for line in lines[:height])


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
        signature = (data['worker'], data['paused'], signature, data.get('diagnostic'), data.get('phase'))
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
