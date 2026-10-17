#!/usr/bin/env python3
"""Multi-project behavior with temporary Git repositories and fake external APIs."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from murphy import bridge as b
from murphy import cli
from murphy.manager import Account, Manager

FAKE = str(ROOT / 'tests/fake_codex.py')


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, HOME=str(self.root))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.user = {'id': 3001, 'login': 'example-owner', 'type': 'User'}
        self.executor = {'id': 2001, 'login': 'example-executor', 'name': 'Example Executor', 'type': 'User'}
        self.repos = {}
        self.comments = {}
        self.reported = []
        self.manager = Manager(self.root / 'state')
        self.manager.timeout = 3
        self.addCleanup(self.manager.close)
        account_api = patch.object(Account, 'api', autospec=True, side_effect=self.account_api)
        account_api.start()
        self.addCleanup(account_api.stop)
        test = self
        class Api:
            def api(self, path, method='GET', payload=None, pages=False, missing_ok=False):
                return test.github_api(path, method, payload, pages, missing_ok)
        api = patch.object(b.GitHub, 'api', Api.api)
        api.start()
        self.addCleanup(api.stop)
        original = b.git
        self.real_git = original
        fetch = patch.object(b, 'git', side_effect=lambda cwd, *args:
                             '' if args[0] == 'fetch' else original(cwd, *args))
        fetch.start()
        self.addCleanup(fetch.stop)
        self.manager.init(gh=sys.executable, codex=FAKE)
        self.previous = b.CONFIG
        self.addCleanup(setattr, b, 'CONFIG', self.previous)

    def account_api(self, account, path, pages=False):
        if path == 'user':
            if account.settings.get('gh_config_dir'):
                return dict(self.executor)
            return dict(self.user)
        if path.startswith('user/repos?'):
            return list(self.repos.values())
        if path.startswith('repos/'):
            return self.repos[path[6:]]
        raise AssertionError(path)

    def github_api(self, path, method='GET', payload=None, pages=False, missing_ok=False):
        repository = b.CONFIG.repository
        self.assertIn(repository, self.repos)
        if path == 'user':
            if b.CONFIG.gh_config_dir:
                return dict(self.executor)
            return dict(self.user)
        if path == 'users/example-owner':
            return {'id': 3001, 'login': 'example-owner', 'type': 'User'}
        if path == 'repos/' + repository:
            return self.repos[repository]
        prefix = 'repos/' + repository + '/issues'
        comments = self.comments[repository]
        if path.startswith(prefix + '/comments?sort=created&direction=desc'):
            return comments[-1:]
        if path.startswith(prefix + '/comments?since='):
            return comments
        if path.startswith(prefix + '/comments/'):
            return next(c for c in comments if c['id'] == int(path.rsplit('/', 1)[1]))
        if b.re.fullmatch(b.re.escape(prefix) + r'/[1-9][0-9]*/parent', path):
            return None
        if b.re.fullmatch(b.re.escape(prefix) + r'/[1-9][0-9]*', path):
            return {'number': int(path.rsplit('/', 1)[1]), 'title': 'Task', 'body': 'Implement this issue.', 'state': 'open',
                    'updated_at': b.now(), 'url': 'https://api.github.com/' + path}
        if b.re.fullmatch(b.re.escape(prefix) + r'/[1-9][0-9]*/comments(?:\?.*)?', path):
            if method == 'POST':
                self.reported.append((repository, payload))
                return {'id': 9000 + len(self.reported)}
            return []
        raise AssertionError((path, method))

    def project_source(self, name, number):
        repository = 'example/' + name
        self.repos[repository] = {'id': number, 'full_name': repository, 'default_branch': 'main',
                                  'permissions': {'push': True}}
        self.comments[repository] = []
        source = self.root / name
        subprocess.run(['git', 'init', '-qb', 'main', str(source)], check=True)
        subprocess.run(['git', '-C', str(source), 'remote', 'add', 'origin',
                        'https://github.com/' + repository + '.git'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test User',
                        '-c', 'user.email=user@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
        subprocess.run(['git', '-C', str(source), 'update-ref', 'refs/remotes/origin/main', 'HEAD'], check=True)
        return source

    def add_project(self, name, number, interval=60):
        source = self.project_source(name, number)
        repository = 'example/' + name
        self.manager.add(repository, source, interval=interval)
        return self.manager.projects[number]

    def comment(self, repository, ident, prompt='read', verb='start', author=3001, item=7):
        comment = {'id': ident, 'body': f'/murphy {verb}\n{prompt}',
                   'issue_url': f'https://api.github.com/repos/{repository}/issues/{item}',
                   'created_at': '2026-10-15T12:00:00Z', 'updated_at': '2026-10-15T12:00:00Z',
                   'user': {'id': author, 'type': 'User'}}
        self.comments[repository].append(comment)
        for _, _, worker in self.manager.projects.values():
            worker.last_poll = float('-inf')
        return comment

    def tick(self):
        b.subreaper()
        with b.lock(self.manager) as fd:
            self.manager.tick(fd)

    @contextlib.contextmanager
    def background(self, timeout=15):
        self.manager.timeout = timeout
        b.subreaper()
        with b.lock(self.manager) as fd:
            try:
                yield fd
            finally:
                self.manager.stop_executions()
        self.assertFalse(b.descendants())

    def wait_for(self, predicate, fd=None):
        deadline = time.monotonic() + 8
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('Timed out waiting for execution: ' + str(self.manager.status()['commands']))
            if fd is not None:
                self.manager.tick(fd, background=True)
            time.sleep(0.02)

    def task_path(self, state, command=1):
        row = state.row(command)
        return Path(json.loads(row['plan'])['cwd']) if row and row['plan'] else state.directory / 'not-started'

    def release(self, state, command=1):
        (self.task_path(state, command) / 'execution-release').touch()

    def test_parallel_projects_respect_global_limit_and_project_order(self):
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        _, third, _ = self.add_project('three', 3)
        for repo, ident, item in (('one', 101, 7), ('one', 102, 8), ('two', 103, 7), ('three', 104, 7)):
            self.comment('example/' + repo, ident, 'WAIT_FOR_TEST_RELEASE', item=item)
        with self.background() as fd:
            self.manager.tick(fd, background=True)
            self.wait_for(lambda: all((self.task_path(s) / 'execution-started').exists()
                                     for s in (first, second)), fd)
            self.assertEqual(set(self.manager.active), {1, 2})
            self.assertEqual(first.row(2)['state'], 'queued')
            self.assertEqual(third.row(1)['state'], 'queued')
            self.release(first)
            self.wait_for(lambda: (self.task_path(first, 2) / 'execution-started').exists(), fd)
            self.assertEqual(set(self.manager.active), {1, 2})
            self.assertGreaterEqual(first.row(2)['started'], first.row(1)['finished'])
            self.release(first, 2)
            self.wait_for(lambda: (self.task_path(third) / 'execution-started').exists(), fd)
            self.assertEqual(set(self.manager.active), {2, 3})
            self.release(second)
            self.release(third)
            self.wait_for(lambda: not self.manager.active, fd)
            self.manager.reports()
        self.assertTrue(all(c['state'] == 'succeeded' for c in self.manager.status()['commands']))
        self.assertEqual(len(self.reported), 4)
        self.assertEqual(len({s.row(1)['thread_id'] for s in (first, second, third)}), 3)
        for repo, payload in self.reported:
            state = {'example/one': first, 'example/two': second, 'example/three': third}[repo]
            self.assertTrue(any(c['execution_id'] in payload['body'] for c in state.db.execute('SELECT * FROM commands')))
        self.assertIs(b.CONFIG, self.previous)

    def test_parallel_limit_one_keeps_projects_serial(self):
        self.manager.max_parallel_projects = 1
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.comment('example/one', 101, 'WAIT_FOR_TEST_RELEASE')
        self.comment('example/two', 102)
        with self.background() as fd:
            self.manager.tick(fd, background=True)
            self.wait_for(lambda: (self.task_path(first) / 'execution-started').exists(), fd)
            self.assertEqual(second.row(1)['state'], 'queued')
            self.release(first)
            self.wait_for(lambda: second.row(1)['state'] == 'succeeded', fd)
            self.assertGreaterEqual(second.row(1)['started'], first.row(1)['finished'])

    def test_parallel_controls_cancel_only_target_and_pause_new_dispatches(self):
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        _, third, _ = self.add_project('three', 3)
        self.comment('example/one', 101, 'WAIT_FOR_TEST_RELEASE DAEMON_FOREVER')
        self.comment('example/two', 102, 'WAIT_FOR_TEST_RELEASE')
        self.comment('example/three', 103)
        with self.background() as fd:
            self.manager.tick(fd, background=True)
            self.wait_for(lambda: all((self.task_path(s) / 'execution-started').exists()
                                     for s in (first, second)), fd)
            model_pid = int((self.task_path(first) / 'execution-started').read_text())
            self.comment('example/three', 104, '', 'pause')['body'] = '/murphy pause'
            self.manager.tick(fd, background=True)
            self.assertEqual(self.manager.setting('paused'), '1')
            self.comment('example/three', 105, '', 'cancel 1')['body'] = '/murphy cancel 1'
            self.wait_for(lambda: first.row(1)['state'] == 'cancelled', fd)
            self.assertFalse(Path(f'/proc/{model_pid}').exists())
            self.assertEqual(second.row(1)['state'], 'running')
            self.assertEqual(third.row(1)['state'], 'queued')
            self.comment('example/three', 106, '', 'resume')['body'] = '/murphy resume'
            self.wait_for(lambda: third.row(1)['state'] == 'succeeded', fd)
            self.assertEqual(second.row(1)['state'], 'running')
            self.release(second)
            self.wait_for(lambda: not self.manager.active, fd)

    def test_executor_crash_drains_orphans_without_killing_another_project(self):
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        _, third, _ = self.add_project('three', 3)
        self.comment('example/one', 101, 'WAIT_FOR_TEST_RELEASE DAEMON_FOREVER')
        self.comment('example/two', 102, 'WAIT_FOR_TEST_RELEASE')
        self.comment('example/three', 103)
        with self.background() as fd:
            self.manager.tick(fd, background=True)
            self.wait_for(lambda: all((self.task_path(s) / 'execution-started').exists()
                                     for s in (first, second)), fd)
            model_pid = int((self.task_path(first) / 'execution-started').read_text())
            self.manager.active[1][0].kill()
            self.wait_for(lambda: first.row(1)['state'] == 'unknown', fd)
            self.assertFalse(Path(f'/proc/{model_pid}').exists())
            self.assertEqual(self.manager.setting('paused'), '1')
            self.assertEqual(second.row(1)['state'], 'running')
            self.assertEqual(third.row(1)['state'], 'queued')
            self.release(second)
            self.wait_for(lambda: not self.manager.active, fd)
        reopened = Manager(self.manager.directory)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.run(once=True), 2)
        self.assertEqual(first.row(1)['state'], 'unknown')
        self.assertEqual(third.row(1)['state'], 'queued')

    def test_waiting_project_frees_slot_and_continues_same_session(self):
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        _, third, _ = self.add_project('three', 3)
        self.comment('example/one', 101, 'ASK_OWNER WRITE_TASK_FILE')
        self.comment('example/two', 102, 'WAIT_FOR_TEST_RELEASE')
        self.comment('example/three', 103, 'WAIT_FOR_TEST_RELEASE')
        with self.background() as fd:
            self.manager.tick(fd, background=True)
            self.wait_for(lambda: (self.task_path(third) / 'execution-started').exists(), fd)
            self.assertEqual(first.row(1)['state'], 'waiting_for_user')
            thread = first.row(1)['thread_id']
            self.comment('example/one', 104, 'Use option A. WRITE_TASK_FILE', 'continue')
            self.manager.tick(fd, background=True)
            self.assertEqual(first.row(2)['state'], 'queued')
            self.release(third)
            self.wait_for(lambda: first.row(2)['state'] == 'succeeded', fd)
            self.assertEqual(first.row(2)['thread_id'], thread)
            self.release(second)
            self.wait_for(lambda: not self.manager.active, fd)

    def test_shutdown_drains_all_active_project_descendants(self):
        states = [self.add_project(name, i)[1] for i, name in ((1, 'one'), (2, 'two'))]
        for i, name in ((101, 'one'), (102, 'two')):
            self.comment('example/' + name, i, 'WAIT_FOR_TEST_RELEASE DAEMON_FOREVER')
        with self.background() as fd:
            self.manager.tick(fd, background=True)
            self.wait_for(lambda: all((self.task_path(s) / 'execution-started').exists() for s in states), fd)
            with self.assertRaisesRegex(ValueError, 'already held'), b.lock(self.manager):
                pass
            self.manager.request_stop()
            self.manager.stop_executions()
            self.assertFalse(self.manager.active)
            self.assertFalse(b.descendants())
            self.assertTrue(all(s.row(1)['state'] == 'cancelled' for s in states))

    def test_parallel_timeout_holds_queue_and_does_not_replay(self):
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.comment('example/one', 101, 'WAIT_FOR_TEST_RELEASE DAEMON_FOREVER')
        self.comment('example/two', 102)
        with self.background(timeout=0.3) as fd:
            self.manager.tick(fd, background=True)
            self.wait_for(lambda: not self.manager.active, fd)
            self.assertEqual(first.row(1)['state'], 'unknown')
            self.assertEqual(second.row(1)['state'], 'succeeded')
            self.assertEqual(self.manager.setting('paused'), '1')
            self.manager.tick(fd, background=True)
            self.assertEqual(len(list(first.directory.glob('*.jsonl'))), 1)

    def test_dirty_source_blocks_dispatch_without_stopping_active_project(self):
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.add_project('three', 3)
        (self.root / 'two/owner.txt').write_text('owner changes')
        self.comment('example/one', 101, 'WAIT_FOR_TEST_RELEASE')
        self.comment('example/two', 102)
        self.comment('example/three', 103)
        with self.background() as fd:
            self.manager.tick(fd, background=True)
            self.wait_for(lambda: second.row(1)['state'] == 'blocked', fd)
            self.wait_for(lambda: (self.task_path(first) / 'execution-started').exists(), fd)
            self.assertEqual(first.row(1)['state'], 'running')
            self.assertEqual(self.manager.setting('paused'), '1')
            self.assertFalse(list(second.directory.glob('*.jsonl')))
            execution = second.row(1)['execution_id']
            (self.root / 'two/owner.txt').unlink()
            self.manager.control('resume')
            self.wait_for(lambda: second.row(1)['state'] == 'succeeded', fd)
            self.assertEqual(second.row(1)['execution_id'], execution)
            self.release(first)
            self.wait_for(lambda: not self.manager.active, fd)

    def test_account_change_stops_and_marks_all_parallel_work_unknown(self):
        states = [self.add_project(name, i)[1] for i, name in ((1, 'one'), (2, 'two'))]
        for i, name in ((101, 'one'), (102, 'two')):
            self.comment('example/' + name, i, 'WAIT_FOR_TEST_RELEASE DAEMON_FOREVER')
        tick = self.manager.tick
        def change(fd, background=False):
            tick(fd, background)
            if all((self.task_path(s) / 'execution-started').exists() for s in states):
                self.user['id'] = 4001
                self.manager.last_identity_check = float('-inf')
        with patch.object(self.manager, 'tick', side_effect=change):
            with self.assertRaisesRegex(ValueError, 'account changed'):
                self.manager.run()
        self.assertTrue(all(s.row(1)['state'] == 'unknown' for s in states))
        self.assertFalse(self.manager.active)
        self.assertFalse(b.descendants())

    def test_coordinator_loss_drains_executors_and_retains_lock_until_safe(self):
        states = [self.add_project(name, i)[1] for i, name in ((1, 'one'), (2, 'two'))]
        for i, name in ((101, 'one'), (102, 'two')):
            self.comment('example/' + name, i, 'WAIT_FOR_TEST_RELEASE DAEMON_FOREVER')
        b.subreaper()
        pid = os.fork()
        if pid == 0:
            try:
                self.manager.close()
                coordinator = Manager(self.manager.directory)
                coordinator.timeout = 15
                coordinator.run()
            finally:
                os._exit(0)
        try:
            self.wait_for(lambda: all((self.task_path(s) / 'execution-started').exists() for s in states))
            for state in states:
                model = (self.task_path(state) / 'execution-started').read_text()
                lock_inode = self.manager.worker_lock.stat().st_ino
                self.assertTrue(any(path.stat().st_ino == lock_inode
                                    for path in Path('/proc', model, 'fd').iterdir()))
            with self.assertRaisesRegex(ValueError, 'already held'), b.lock(self.manager):
                pass
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            self.wait_for(lambda: all(s.row(1)['state'] == 'unknown' for s in states))
            # Executors have recorded their uncertain outcome; reap their exits.
            self.manager.drain_orphans()
            with b.lock(self.manager):
                self.assertFalse(b.descendants())
            self.assertEqual(self.manager.setting('paused'), '1')
            self.assertTrue(all(len(list(s.directory.glob('*.jsonl'))) == 1 for s in states))
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
            self.manager.drain_orphans()

    def test_interrupted_dispatch_reservation_is_unknown_after_restart(self):
        _, state, _ = self.add_project('one', 1)
        self.comment('example/one', 101)
        self.manager.pump()
        with state.db:
            state.db.execute("UPDATE commands SET state='running' WHERE id=1")
        reopened = Manager(self.manager.directory)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.run(once=True), 2)
        self.assertEqual(state.row(1)['state'], 'unknown')
        self.assertFalse(list(state.directory.glob('*.jsonl')))

    def test_failed_executor_launch_is_unknown_and_never_requeued(self):
        _, state, _ = self.add_project('one', 1)
        self.comment('example/one', 101)
        with self.background() as fd:
            with patch('multiprocessing.process.BaseProcess.start', side_effect=RuntimeError('fork failed')):
                with self.assertRaisesRegex(RuntimeError, 'fork failed'):
                    self.manager.tick(fd, background=True)
            self.assertEqual(state.row(1)['state'], 'unknown')
            self.assertEqual(self.manager.setting('paused'), '1')
            self.manager.tick(fd, background=True)
            self.assertFalse(self.manager.active)
            self.assertFalse(list(state.directory.glob('*.jsonl')))

    def test_cancelled_dispatch_does_not_invoke_model(self):
        _, state, _ = self.add_project('one', 1)
        self.comment('example/one', 101)
        self.manager.pump()
        with state.db:
            state.db.execute('UPDATE commands SET cancel=1 WHERE id=1')
        with self.background() as fd:
            self.manager.start_execution(1, 1, fd)
            self.wait_for(lambda: not self.manager.active, fd)
        self.assertEqual(state.row(1)['state'], 'cancelled')
        self.assertFalse(list(state.directory.glob('*.jsonl')))
        self.assertIsNone(state.row(1)['plan'])

    def test_invalid_parallel_limit_rejected_before_external_work(self):
        for value in (0, -1, 1.5):
            self.manager.max_parallel_projects = value
            with patch.object(self.manager.account, 'verify') as verify:
                with self.assertRaisesRegex(ValueError, 'Maximum parallel projects'):
                    self.manager.run()
                verify.assert_not_called()

    def test_separate_executor_identity_survives_parallel_project_processes(self):
        self.manager = self.separate_manager()
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        for i, name in ((101, 'one'), (102, 'two')):
            self.comment('example/' + name, i, 'WAIT_FOR_TEST_RELEASE COMMIT_EXECUTOR_FILE')
        self.user = {'id': 4001, 'login': 'another-owner', 'type': 'User'}
        with self.background() as fd:
            self.manager.tick(fd, background=True)
            self.wait_for(lambda: all((self.task_path(s) / 'execution-started').exists()
                                     for s in (first, second)), fd)
            self.release(first)
            self.release(second)
            self.wait_for(lambda: not self.manager.active, fd)
        for state in (first, second):
            self.assertEqual(state.row(1)['state'], 'succeeded')
            author = subprocess.check_output(['git', '-C', str(self.task_path(state)), 'log', '-1',
                                              '--format=%an <%ae>|%cn <%ce>'], text=True).strip()
            self.assertEqual(author, 'Example Executor <2001+example-executor@users.noreply.github.com>|'
                                     'Example Executor <2001+example-executor@users.noreply.github.com>')
        self.assertNotEqual(first.row(1)['thread_id'], second.row(1)['thread_id'])

    def test_automatic_identity_and_same_account_configuration(self):
        config, state, _ = self.add_project('one', 1)
        self.assertEqual(config.owner_user_id, 3001)
        self.assertEqual(config.bot_user_id, config.owner_user_id)
        self.assertEqual(state.setting('baseline'), '0')
        with self.assertRaisesRegex(ValueError, 'Already initialized'):
            self.manager.init()
        with self.assertRaisesRegex(ValueError, 'already connected'):
            self.manager.add('example/one', self.root / 'one')

    def test_project_intervals_survive_restart_and_are_visible(self):
        self.add_project('one', 1)
        self.add_project('two', 2, interval=120)
        expected = [{'id': 1, 'repository': 'example/one', 'interval': 60},
                    {'id': 2, 'repository': 'example/two', 'interval': 120}]
        self.assertEqual(self.manager.status()['projects'], expected)
        reopened = Manager(self.manager.directory)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.status()['projects'], expected)
        self.assertEqual([worker.poll_seconds for _, _, worker in reopened.projects.values()], [60, 120])

    def test_add_cli_accepts_default_short_and_long_interval_options(self):
        for number, options in ((1, []), (2, ['-i', '15']), (3, ['--interval', '120'])):
            source = self.project_source(str(number), number)
            argv = ['murphy', '--state-dir', str(self.manager.directory), 'projects', 'add',
                    'example/' + str(number), '--checkout', str(source), *options]
            with patch.object(sys, 'argv', argv), patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(cli.main(), 0)
        self.assertEqual([project['interval'] for project in self.manager.status()['projects']], [60, 15, 120])

    def test_project_polling_obeys_independent_intervals(self):
        self.add_project('one', 1, interval=20)
        self.add_project('two', 2, interval=60)
        self.comment('example/one', 101)
        self.comment('example/two', 102)
        with patch.object(self, 'github_api', wraps=self.github_api) as api:
            with patch('murphy.manager.time.monotonic', return_value=0) as clock:
                for instant in (0, 19, 20, 39, 40, 59, 60):
                    clock.return_value = instant
                    self.manager.pump()
        polls = [call.args[0] for call in api.call_args_list if '/comments?since=' in call.args[0]]
        self.assertEqual(sum('/example/one/' in path for path in polls), 4)
        self.assertEqual(sum('/example/two/' in path for path in polls), 2)
        self.assertEqual(len(self.manager.status()['commands']), 2)  # Repeated discovery stays deduplicated.

    def test_old_project_registry_gains_default_without_losing_state(self):
        _, state, _ = self.add_project('one', 1)
        self.comment('example/one', 101)
        self.tick()
        expected = self.manager.status()
        baseline = state.setting('baseline')
        self.manager.close()
        with sqlite3.connect(self.root / 'state/manager.sqlite') as old:
            old.execute('ALTER TABLE projects DROP COLUMN poll_interval')
        reopened = Manager(self.root / 'state')
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.status(), expected)
        self.assertEqual(reopened.projects[1][1].setting('baseline'), baseline)
        self.assertEqual(reopened.projects[1][2].poll_seconds, 60)

    def test_run_override_does_not_change_saved_project_intervals(self):
        self.add_project('one', 1, interval=120)
        reopened = Manager(self.manager.directory)
        self.addCleanup(reopened.close)
        reopened.poll_seconds = 5
        self.assertEqual(reopened.status()['projects'][0]['interval'], 120)
        self.assertEqual(reopened.projects[1][2].poll_seconds, 5)
        normal = Manager(self.manager.directory)
        self.addCleanup(normal.close)
        normal.load()
        self.assertEqual(normal.projects[1][2].poll_seconds, 120)

    def test_invalid_project_interval_has_no_setup_side_effects(self):
        with patch.object(self.manager.account, 'verify') as verify:
            for interval in (0, -1, 1.5, True):
                with self.assertRaisesRegex(ValueError, 'whole number of seconds'):
                    self.manager.add('example/one', interval=interval)
            verify.assert_not_called()
        self.assertEqual(self.manager.status()['projects'], [])

    def separate_manager(self):
        manager = Manager(self.root / 'separate-state')
        self.addCleanup(manager.close)
        with patch('murphy.auth.login') as login:
            manager.init(gh=sys.executable, codex=FAKE, separate_account=True)
        self.assertEqual(login.call_count, 1)
        return manager

    def test_separate_executor_accepts_only_owner_commands_across_projects(self):
        self.manager = self.separate_manager()
        config, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.assertEqual((config.owner_user_id, config.bot_user_id), (3001, 2001))
        self.comment('example/one', 101, author=2001)
        self.comment('example/one', 102, 'COMMIT_EXECUTOR_FILE', author=3001)
        self.comment('example/two', 103, author=3001)
        # Switching the owner's ordinary gh account must not affect the executor.
        self.user = {'id': 4001, 'login': 'another-owner', 'type': 'User'}
        self.tick()
        self.tick()
        self.manager.reports()
        self.assertEqual(first.row(1)['state'], 'succeeded')
        self.assertEqual(second.row(1)['state'], 'succeeded')
        self.assertEqual(first.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 1)
        cwd = json.loads(first.row(1)['plan'])['cwd']
        author = subprocess.check_output(['git', '-C', cwd, 'log', '-1', '--format=%an <%ae>|%cn <%ce>'], text=True)
        self.assertEqual(author.strip(), 'Example Executor <2001+example-executor@users.noreply.github.com>|'
                         'Example Executor <2001+example-executor@users.noreply.github.com>')
        self.assertEqual({repo for repo, _ in self.reported}, {'example/one', 'example/two'})
        self.assertTrue(self.manager.auth_status()['authenticated'])

    def test_separate_executor_status_and_reauthentication_do_not_rebind_identity(self):
        manager = self.separate_manager()
        status = manager.auth_status()
        self.assertEqual(status['mode'], 'separate')
        self.assertEqual((status['owner']['id'], status['executor']['id']), (3001, 2001))
        self.executor['id'] = 2002
        self.assertFalse(manager.auth_status()['authenticated'])
        with patch('murphy.auth.login'), self.assertRaisesRegex(ValueError, 'account changed'):
            manager.login()
        self.assertEqual(manager.account.settings['user_id'], 2001)
        self.executor['id'] = 2001
        with patch('murphy.auth.login') as login:
            manager.login()
        self.assertTrue(manager.auth_status()['authenticated'])
        with b.lock(manager), self.assertRaisesRegex(ValueError, 'already held'):
            manager.login()
        self.assertEqual(login.call_count, 1)

    def test_same_account_and_failed_separate_login_are_not_saved(self):
        other = Manager(self.root / 'failed-setup')
        self.addCleanup(other.close)
        self.executor = dict(self.user)
        with patch('murphy.auth.login'), self.assertRaisesRegex(ValueError, 'different executor'):
            other.init(gh=sys.executable, codex=FAKE, separate_account=True)
        self.assertIsNone(other.setting('account'))
        with patch('murphy.auth.login', side_effect=RuntimeError('login failed')), self.assertRaises(RuntimeError):
            other.init(gh=sys.executable, codex=FAKE, separate_account=True)
        self.assertIsNone(other.setting('account'))

    def test_separate_executor_change_during_turn_drains_and_pauses(self):
        self.manager = self.separate_manager()
        _, state, worker = self.add_project('one', 1)
        self.comment('example/one', 101, 'SLOW')
        original = worker.poll
        def switch():
            self.executor['id'] = 2002
            self.manager.last_identity_check = float('-inf')
            original()
        worker.poll = switch
        self.tick()
        self.assertEqual(state.row(1)['state'], 'unknown')
        self.assertEqual(self.manager.setting('paused'), '1')
        self.assertFalse(b.descendants())

    def test_two_projects_same_issue_number_keep_sessions_and_global_fifo(self):
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.comment('example/one', 102)
        self.comment('example/two', 101, 'DESCENDANT')
        self.tick()
        self.assertEqual(second.row(1)['state'], 'succeeded')
        self.assertEqual(first.row(1)['state'], 'queued')
        self.tick()
        self.assertEqual(first.row(1)['state'], 'succeeded')
        self.assertGreaterEqual(first.row(1)['started'], second.row(1)['finished'])
        self.assertNotEqual(first.row(1)['thread_id'], second.row(1)['thread_id'])
        self.assertNotEqual(json.loads(first.row(1)['plan'])['cwd'], json.loads(second.row(1)['plan'])['cwd'])
        self.manager.reports()
        self.assertEqual({repo for repo, _ in self.reported}, {'example/one', 'example/two'})
        for state in (first, second):
            self.assertTrue(any('<!-- codex-bridge:' + state.row(1)['execution_id'] + ' -->' in payload['body']
                                for _, payload in self.reported))
        self.comment('example/two', 103, 'WRITE_TASK_FILE', 'continue')
        self.tick()
        self.assertEqual(second.row(2)['thread_id'], second.row(1)['thread_id'])
        self.assertEqual(second.row(2)['state'], 'succeeded')
        self.assertIs(b.CONFIG, self.previous)

    def test_waiting_issue_survives_restart_and_answer_queues_behind_active_issue(self):
        _, state, _ = self.add_project('one', 1)
        self.comment('example/one', 101, 'ASK_OWNER WRITE_TASK_FILE', item=7)
        self.comment('example/one', 102, 'SLOW', item=8)
        self.tick()
        first = state.row(1)
        self.assertEqual(first['state'], 'waiting_for_user')
        self.assertEqual(state.row(2)['state'], 'queued')
        self.assertEqual(self.manager.setting('paused'), '0')
        self.manager.reports()
        self.manager.reports()
        self.assertEqual(len(self.reported), 1)
        self.assertIn('Specify which option to use: A or B.', self.reported[0][1]['body'])
        self.manager.close()
        self.manager = Manager(self.root / 'state')
        self.addCleanup(self.manager.close)
        self.manager.load()
        _, state, worker = self.manager.projects[1]
        self.manager.control('resume')  # A generic resume is not an answer/replay.
        self.assertEqual(state.row(1)['state'], 'waiting_for_user')
        self.comment('example/one', 103)['body'] = 'Option A.'
        self.comment('example/one', 104, 'wrong account answer', verb='continue', author=4001)
        original = worker.poll
        sent = False
        def answer_while_other_issue_runs():
            nonlocal sent
            if not sent:
                sent = True
                self.assertEqual(state.row(2)['state'], 'running')
                self.comment('example/one', 105, 'Option A. WRITE_TASK_FILE', verb='continue')
            original()
        worker.poll = answer_while_other_issue_runs
        self.tick()
        self.assertEqual(state.row(2)['state'], 'succeeded')
        self.assertEqual(state.row(1)['state'], 'waiting_for_user')
        self.assertEqual(state.row(3)['state'], 'queued')
        self.assertEqual(state.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 3)
        self.tick()
        reply = state.row(3)
        self.assertEqual(reply['state'], 'succeeded')
        self.assertEqual(reply['thread_id'], first['thread_id'])
        self.assertNotEqual(state.row(2)['thread_id'], first['thread_id'])
        self.assertGreaterEqual(reply['started'], state.row(2)['finished'])
        self.assertEqual(reply['task'], first['task'])
        self.assertNotEqual(json.loads(first['plan'])['cwd'], json.loads(state.row(2)['plan'])['cwd'])
        cwd = Path(json.loads(first['plan'])['cwd'])
        self.assertEqual((cwd / 'task.txt').read_text(), 'task change\ntask change\n')
        self.assertEqual(self.manager.setting('paused'), '0')

    def test_waiting_report_outage_does_not_replay_or_hold_another_project(self):
        _, first, worker = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.comment('example/one', 101, 'ASK_OWNER')
        self.comment('example/two', 102)
        self.tick()
        with patch.object(worker.github, 'report', side_effect=RuntimeError('network outage')):
            self.tick()
        self.assertEqual(first.row(1)['state'], 'waiting_for_user')
        self.assertEqual(second.row(1)['state'], 'succeeded')
        self.assertIsNone(first.row(1)['report_id'])
        worker.report_retry_after = 0
        self.manager.reports()
        self.manager.reports()
        waiting_reports = [(repo, payload) for repo, payload in self.reported
                           if 'Specify which option to use: A or B.' in payload['body']]
        self.assertEqual(len(waiting_reports), 1)
        self.assertEqual(waiting_reports[0][0], 'example/one')
        self.assertEqual(len(list(first.directory.glob('*.jsonl'))), 1)
        self.assertEqual(self.manager.setting('paused'), '0')
        self.assertIs(b.CONFIG, self.previous)

    def test_only_launch_account_commands_and_new_unedited_comments(self):
        _, state, _ = self.add_project('one', 1)
        self.comment('example/one', 101, author=4001)
        edited = self.comment('example/one', 102)
        edited['updated_at'] = '2026-10-15T12:01:00Z'
        self.tick()
        self.assertEqual(state.db.execute('SELECT COUNT(*) FROM commands').fetchone()[0], 0)
        self.comment('example/one', 103)
        self.manager.pump()
        self.comments['example/one'][-1]['body'] += ' changed'
        self.tick()
        self.assertEqual(state.row(1)['state'], 'cancelled')
        self.assertFalse(list(state.directory.glob('*.jsonl')))

    def test_account_change_blocks_other_projects_and_reports(self):
        _, state, _ = self.add_project('one', 1)
        self.comment('example/one', 101)
        # Fresh CI machines can have less uptime than the identity interval.
        with patch('murphy.manager.time.monotonic', return_value=0):
            self.manager.pump()
            self.user['id'] = 4001
            self.manager.last_identity_check = float('-inf')
            with self.assertRaisesRegex(ValueError, 'account changed'):
                self.tick()
        self.assertEqual(state.row(1)['state'], 'queued')
        self.assertFalse(self.reported)

    def test_account_change_during_turn_drains_and_marks_unknown(self):
        _, state, worker = self.add_project('one', 1)
        self.comment('example/one', 101, 'SLOW')
        original = worker.poll
        def switch():
            self.user['id'] = 4001
            self.manager.last_identity_check = float('-inf')
            original()
        worker.poll = switch
        self.tick()
        self.assertEqual(state.row(1)['state'], 'unknown')
        self.assertEqual(self.manager.setting('paused'), '1')
        self.assertFalse(b.descendants())

    def test_replaced_repository_cannot_execute_a_saved_command(self):
        _, state, _ = self.add_project('one', 1)
        self.comment('example/one', 101)
        self.manager.pump()
        self.repos['example/one']['id'] = 99
        self.tick()
        self.assertEqual(state.row(1)['state'], 'cancelled')
        self.assertFalse(list(state.directory.glob('*.jsonl')))

    def test_temporary_identity_check_failure_does_not_kill_an_active_turn(self):
        _, state, worker = self.add_project('one', 1)
        self.comment('example/one', 101, 'SLOW')
        original = worker.poll
        def outage():
            self.manager.last_identity_check = float('-inf')
            with patch.object(self.manager.account, 'verify', side_effect=RuntimeError('temporary API outage')):
                original()
        worker.poll = outage
        self.tick()
        self.assertEqual(state.row(1)['state'], 'succeeded')

    def test_identity_outage_retains_queued_commands_until_recovery(self):
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.comment('example/one', 101)
        self.comment('example/two', 102)
        self.manager.pump()
        order = self.manager.status()['commands']
        self.manager.last_identity_check = float('-inf')
        with patch.object(self.manager.account, 'verify', side_effect=RuntimeError('executor unavailable')) as verify:
            self.tick()
            self.tick()  # The retry interval must not reopen scheduling after a failure.
            self.assertEqual(verify.call_count, 1)
        self.assertEqual(first.row(1)['state'], 'queued')
        self.assertEqual(second.row(1)['state'], 'queued')
        self.assertFalse(list(first.directory.glob('*.jsonl')))
        self.assertFalse(list(second.directory.glob('*.jsonl')))
        self.assertFalse(self.reported)
        self.manager.last_identity_check = float('-inf')
        self.tick()
        self.tick()
        self.assertEqual(first.row(1)['state'], 'succeeded')
        self.assertEqual(second.row(1)['state'], 'succeeded')
        self.assertEqual([row['id'] for row in self.manager.status()['commands']], [row['id'] for row in order])

    def test_unknown_in_one_project_blocks_all_until_explicit_resolution(self):
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.comment('example/one', 101, 'NO_COMPLETION')
        self.comment('example/two', 102)
        self.tick()
        self.assertEqual(first.row(1)['state'], 'unknown')
        self.tick()
        self.assertEqual(second.row(1)['state'], 'queued')
        with self.assertRaisesRegex(ValueError, 'every project'):
            self.manager.control('resume')
        self.manager.resolve(1, 'failed', 'Inspected logs and descendants.')
        self.manager.control('resume')
        self.tick()
        self.assertEqual(second.row(1)['state'], 'succeeded')

    def test_dirty_source_pauses_reports_once_survives_restart_and_resumes_from_github(self):
        _, first, worker = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        dirty = self.root / 'one/draft.txt'
        dirty.write_text('private owner contents\n')
        self.comment('example/one', 101)
        self.comment('example/two', 102)
        self.tick()
        blocked = first.row(1)
        self.assertEqual(blocked['state'], 'blocked')
        self.assertEqual(second.row(1)['state'], 'queued')
        self.assertEqual(self.manager.setting('paused'), '1')
        self.assertFalse(list(first.directory.glob('*.jsonl')))
        with patch.object(worker.github, 'report', side_effect=RuntimeError('network outage')):
            self.manager.reports()
        worker.report_retry_after = 0
        self.manager.reports()
        self.manager.reports()
        self.assertEqual(len(self.reported), 1)
        self.assertEqual(self.reported[0][0], 'example/one')
        self.assertIn('Source checkout contains uncommitted changes', self.reported[0][1]['body'])
        self.assertIn('draft.txt', self.reported[0][1]['body'])
        self.assertNotIn('private owner contents', self.reported[0][1]['body'])
        other = Manager(self.manager.directory)
        try:
            self.assertEqual(other.run(once=True), 0)
        finally:
            other.close()
        self.assertEqual(first.row(1)['state'], 'blocked')
        self.assertEqual(len(self.reported), 1)
        dirty.unlink()  # The owner cleans up; the worker itself must not do so.
        self.tick()
        self.assertEqual(first.row(1)['state'], 'blocked')
        self.comment('example/two', 103)['body'] = '/murphy resume'
        self.tick()
        self.assertEqual(first.row(1)['state'], 'succeeded')
        self.assertEqual(first.row(1)['execution_id'], blocked['execution_id'])
        self.assertEqual(second.row(1)['state'], 'queued')
        self.assertEqual(self.manager.setting('paused'), '0')
        self.assertEqual(len(list(first.directory.glob('*.jsonl'))), 1)

    def test_blocked_command_can_be_cancelled_without_model_work(self):
        _, state, _ = self.add_project('one', 1)
        (self.root / 'one/draft.txt').write_text('private draft\n')
        self.comment('example/one', 101)
        self.tick()
        self.manager.reports()
        self.assertIsNotNone(state.row(1)['report_id'])
        self.manager.control('cancel', '1')
        self.assertEqual(state.row(1)['state'], 'cancelled')
        self.assertIsNone(state.row(1)['report_id'])
        self.assertFalse(list(state.directory.glob('*.jsonl')))

    def test_registered_source_allows_its_own_saved_uncommitted_changes(self):
        _, state, _ = self.add_project('one', 1)
        self.manager.register('example/one', 'manual', self.root / 'one', 'main', [7])
        self.manager.enqueue('example/one', 'manual', 'start', 'WRITE_TASK_FILE')
        self.tick()
        self.manager.enqueue('example/one', 'manual', 'continue', 'WRITE_TASK_FILE')
        self.tick()
        self.assertEqual(state.row(1)['state'], 'succeeded')
        self.assertEqual(state.row(2)['state'], 'succeeded')
        self.assertEqual(state.row(1)['thread_id'], state.row(2)['thread_id'])
        self.assertEqual((self.root / 'one/task.txt').read_text(), 'task change\ntask change\n')

    def test_restart_recovers_running_project_and_preserves_order(self):
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.comment('example/one', 101)
        self.comment('example/two', 102)
        self.manager.pump()
        with first.db:
            first.db.execute("UPDATE commands SET state='running' WHERE id=1")
        order = self.manager.status()['commands']
        other = Manager(self.manager.directory)
        try:
            self.assertEqual(other.run(once=True), 2)
            self.assertEqual([r['id'] for r in order], [r['id'] for r in other.status()['commands']])
            self.assertEqual(second.row(1)['state'], 'queued')
            self.assertEqual(first.row(1)['state'], 'unknown')
        finally:
            other.close()

    def test_global_controls_from_another_project_work_during_a_turn(self):
        _, first, active = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.comment('example/one', 101, 'SLOW DAEMON_FOREVER')
        self.manager.pump()
        original = active.poll
        sent = False
        def cancel():
            nonlocal sent
            if not sent:
                sent = True
                self.comment('example/two', 102, '', 'cancel 1')
                self.comments['example/two'][-1]['body'] = '/murphy cancel 1'
            original()
        active.poll = cancel
        self.tick()
        self.assertEqual(first.row(1)['state'], 'cancelled')
        self.assertEqual(second.row(1)['state'], 'succeeded')
        self.assertFalse(b.descendants())

    def test_report_retry_never_repeats_execution(self):
        _, state, worker = self.add_project('one', 1)
        self.comment('example/one', 101)
        self.tick()
        with patch.object(worker.github, 'report', side_effect=RuntimeError('network outage')):
            self.manager.reports()
        worker.report_retry_after = 0
        self.tick()
        self.assertEqual(state.row(1)['report_id'], 9001)
        self.assertEqual(len(list(state.directory.glob('*.jsonl'))), 1)

    def test_pause_and_cancel_ids_are_global(self):
        _, first, _ = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.comment('example/one', 101)
        self.comment('example/two', 102)
        self.manager.control('pause')
        self.tick()
        self.manager.control('cancel', '2')
        self.assertEqual(first.row(1)['state'], 'queued')
        self.assertEqual(second.row(1)['state'], 'cancelled')
        self.manager.control('resume')
        self.tick()
        self.assertEqual(first.row(1)['state'], 'succeeded')

    def test_local_registration_and_alias_continuation_use_the_same_session(self):
        _, state, _ = self.add_project('one', 1)
        self.manager.register('example/one', 'manual', self.root / 'one', 'main', [7, 8], reasoning='medium')
        self.assertEqual(self.manager.enqueue('example/one', 'manual', 'start', 'read'), 1)
        self.tick()
        self.assertEqual(state.row(1)['state'], 'succeeded')
        continuation = self.comment('example/one', 101, 'read', 'continue')
        continuation['issue_url'] = continuation['issue_url'].replace('/issues/7', '/issues/8')
        self.assertEqual(self.manager.ingest('example/one', 101), [2])
        self.tick()
        self.assertEqual(state.row(2)['state'], 'succeeded')
        self.assertEqual(state.row(1)['thread_id'], state.row(2)['thread_id'])
        self.assertEqual(state.row(2)['reasoning'], 'medium')

    def test_stop_drains_active_turn_and_leaves_other_project_queued(self):
        _, first, active = self.add_project('one', 1)
        _, second, _ = self.add_project('two', 2)
        self.comment('example/one', 101, 'SLOW DAEMON_FOREVER')
        self.comment('example/two', 102)
        active.poll = self.manager.request_stop
        self.tick()
        self.assertEqual(first.row(1)['state'], 'cancelled')
        self.assertEqual(second.row(1)['state'], 'queued')
        self.assertFalse(b.descendants())

    def test_managed_clone_uses_gh_and_does_not_reuse_existing_directory(self):
        self.add_project('seed', 1)
        repo = 'example/cloned'
        self.repos[repo] = {'id': 2, 'full_name': repo, 'default_branch': 'main', 'permissions': {'push': True}}
        self.comments[repo] = []
        original = subprocess.run
        def clone(args, **kwargs):
            if args[:3] != [sys.executable, 'repo', 'clone']:
                return original(args, **kwargs)
            self.assertEqual(args[:4], [sys.executable, 'repo', 'clone', 'https://github.com/' + repo])
            result = original(['git', 'clone', '-q', str(self.root / 'seed'), args[4]], check=True)
            original(['git', '-C', args[4], 'remote', 'set-url', 'origin', 'https://github.com/' + repo], check=True)
            return result
        with patch.object(subprocess, 'run', side_effect=clone):
            self.assertEqual(self.manager.add(repo), repo)
        source = self.manager.directory / 'checkouts/2'
        self.assertTrue((source / '.git').is_dir())

    def test_single_branch_checkout_fetches_and_refreshes_explicit_task_base(self):
        self.add_project('seed', 1)
        seed = self.root / 'seed'
        remote = self.root / 'remote.git'
        source = self.root / 'single'
        subprocess.run(['git', 'clone', '--bare', '-q', str(seed), str(remote)], check=True)
        subprocess.run(['git', '-C', str(seed), 'switch', '-qc', 'develop'], check=True)
        subprocess.run(['git', '-C', str(seed), 'push', '-q', str(remote), 'develop'], check=True)
        subprocess.run(['git', 'clone', '-q', '--single-branch', '--branch', 'main', str(remote), str(source)],
                       check=True)
        repo = 'example/single'
        self.repos[repo] = {'id': 2, 'full_name': repo, 'default_branch': 'main', 'permissions': {'push': True}}
        self.comments[repo] = []
        subprocess.run(['git', '-C', str(source), 'remote', 'set-url', 'origin', 'https://github.com/' + repo],
                       check=True)
        original_config = (source / '.git/config').read_bytes()
        def local_fetch(cwd, *args):
            if args[0] == 'fetch':
                return self.real_git(cwd, '-c', f'url.{remote}.insteadOf=https://github.com/{repo}', *args)
            return self.real_git(cwd, *args)
        with patch.object(b, 'git', side_effect=local_fetch):
            self.manager.add(repo, source, base='develop')
            before = subprocess.check_output(['git', '-C', str(source), 'rev-parse',
                                              'refs/remotes/origin/develop'], text=True).strip()
            subprocess.run(['git', '-C', str(seed), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'new base'],
                           check=True)
            subprocess.run(['git', '-C', str(seed), 'push', '-q', str(remote), 'develop'], check=True)
            expected = subprocess.check_output(['git', '-C', str(seed), 'rev-parse', 'HEAD'], text=True).strip()
            self.assertNotEqual(before, expected)
            self.comment(repo, 101)
            self.tick()
        _, state, _ = self.manager.projects[2]
        self.assertEqual(state.row(1)['state'], 'succeeded', state.row(1)['detail'])
        self.assertEqual(state.row(1)['head'], expected)
        self.assertEqual(subprocess.check_output(['git', '-C', str(source), 'branch', '--show-current'],
                                                 text=True).strip(), 'main')
        self.assertEqual((source / '.git/config').read_bytes(), original_config)

    def test_available_projects_and_missing_prerequisites(self):
        self.add_project('one', 1)
        self.assertEqual(self.manager.available(), [{'repository': 'example/one', 'can_push': True}])
        from murphy.manager import prerequisite
        with self.assertRaisesRegex(ValueError, 'Missing executable'):
            prerequisite('/missing/example/tool')


if __name__ == '__main__':
    unittest.main(verbosity=2)
