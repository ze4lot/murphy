#!/usr/bin/env python3
"""Behavior checks for auth, durable routing and actual Linux process serialization."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('bridge', ROOT.parent / 'bridge.py')
b = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = b
spec.loader.exec_module(b)
FIXTURE_CONFIG = {'repository': 'example/project', 'repository_id': 1001, 'bot_user_id': 2001,
                  'owner_user_id': 3001, 'owner_login': 'example-owner'}
b.CONFIG = b.Config(**FIXTURE_CONFIG)
FAKE = str(ROOT / 'fake_codex.py')


def comment(ident, body='/murphy start', item=226, author=b.CONFIG.owner_user_id, **extra):
    data = {'id': ident, 'body': body, 'issue_url': f'https://api.github.com/repos/{b.CONFIG.repository}/issues/{item}',
            'created_at': '2026-10-15T12:00:00Z', 'updated_at': '2026-10-15T12:00:00Z',
            'user': {'id': author, 'type': 'User'}}
    return dict(data, **extra)


class Api:
    def __init__(self):
        self.data = {}
        self.report_calls = []
        self.issues = {}

    def identity(self):
        pass

    def latest(self):
        return comment(100)

    def comments(self, since):
        return list(self.data.values())

    def comment(self, ident):
        return self.data[ident]

    def issue(self, number):
        return self.issues[number]

    def start_base(self, number, default):
        return default

    def report(self, row):
        self.report_calls.append(dict(row))
        return 1000 + row['id']


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config_path = root / 'config.json'
        self.config_path.write_text(json.dumps(FIXTURE_CONFIG))
        b.configure(self.config_path)
        self.cwd = root / 'checkout'
        self.cwd.mkdir()
        self.state = b.State(root / 'state')
        self.addCleanup(self.state.db.close)
        self.state.bind_config(b.CONFIG)
        self.api = Api()
        self.state.initialize(self.api)
        subprocess.run([b.CONFIG.git, 'init', '-q', '-b', 'codex/pilot', str(self.cwd)], check=True)
        subprocess.run([b.CONFIG.git, '-C', str(self.cwd), 'remote', 'add', 'origin', 'git@github.com:example/project.git'], check=True)
        subprocess.run([b.CONFIG.git, '-C', str(self.cwd), '-c', 'user.name=Test Bot', '-c', 'user.email=bot@example.invalid', 'commit', '--allow-empty', '-qm', 'isolated process fixture'], check=True)
        self.state.register('a', self.cwd, 'codex/pilot', [226, 229])
        self.state.register('b', self.cwd, 'codex/pilot')
        self.worker = b.Worker(self.state, codex=FAKE, timeout=3)

    def execute(self, once=True):
        self.worker.run(once=once)

    def test_cli_loads_config_and_refuses_a_different_project_state(self):
        args = [sys.executable, str(ROOT.parent / 'bridge.py'), '--config', str(self.config_path),
                '--state-dir', str(self.state.directory), 'status']
        env = dict(os.environ, HOME=self.temp.name)
        result = subprocess.run(args, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['tasks']), 2)
        self.config_path.write_text(json.dumps(dict(FIXTURE_CONFIG, repository='example/other')))
        result = subprocess.run(args, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('another repository', result.stderr)
        self.assertEqual(self.state.setting('identity'), json.dumps(FIXTURE_CONFIG, sort_keys=True))

    def test_cli_requires_explicit_config_before_touching_state(self):
        result = subprocess.run([sys.executable, str(ROOT.parent / 'bridge.py'), 'status'],
                                env=dict(os.environ, HOME=self.temp.name), capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--config', result.stderr)
        self.assertFalse((Path(self.temp.name) / '.local').exists())

    def test_config_loads_plain_settings_and_configurable_tools(self):
        settings = dict(FIXTURE_CONFIG, git='/opt/example tools/git', gh='custom-gh', codex='custom-codex')
        self.config_path.write_text(json.dumps(settings))
        config = b.configure(self.config_path)
        self.assertEqual(config.repository, 'example/project')
        self.assertEqual(config.git, '/opt/example tools/git')
        self.assertEqual(config.gh, 'custom-gh')
        self.assertEqual(config.codex, 'custom-codex')

    def test_invalid_config_and_secret_fields_are_rejected(self):
        cases = [dict(FIXTURE_CONFIG, repository='../project'),
                 dict(FIXTURE_CONFIG, repository='example/..'),
                 dict(FIXTURE_CONFIG, repository_id=True),
                 dict(FIXTURE_CONFIG, owner_user_id=0),
                 dict(FIXTURE_CONFIG, bot_user_id=3001),
                 dict(FIXTURE_CONFIG, owner_login='../owner'),
                 dict(FIXTURE_CONFIG, gh=''),
                 dict(FIXTURE_CONFIG, token='not-a-real-token')]
        for settings in cases:
            with self.subTest(settings=settings):
                self.config_path.write_text(json.dumps(settings))
                with self.assertRaises(ValueError):
                    b.configure(self.config_path)

    def test_state_is_pinned_to_repository_and_authorization(self):
        original = self.state.setting('identity')
        for changes in ({'repository': 'example/another'}, {'repository_id': 1002},
                        {'owner_user_id': 3002}, {'bot_user_id': 2002}):
            config = b.Config(**dict(FIXTURE_CONFIG, **changes))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.state.bind_config(config)
            self.assertEqual(self.state.setting('identity'), original)
        self.state.bind_config(b.CONFIG)
        self.assertEqual(self.state.setting('identity'), original)

    def test_unscoped_legacy_state_is_never_silently_adopted(self):
        with self.state.db:
            self.state.db.execute("DELETE FROM settings WHERE key='identity'")
        with self.assertRaisesRegex(ValueError, 'fresh state'):
            self.state.bind_config(b.CONFIG)
        self.assertIsNone(self.state.setting('identity'))

    def test_github_identity_checks_configured_bot_repo_and_human(self):
        api = b.GitHub()
        replies = [{'id': 2001}, {'id': 1001, 'full_name': 'example/project'},
                   {'id': 3001, 'type': 'User'}]
        with patch.object(api, 'api', side_effect=replies) as call:
            api.identity()
        self.assertEqual([c.args[0] for c in call.call_args_list],
                         ['user', 'repos/example/project', 'users/example-owner'])
        for index in range(3):
            changed = [dict(r) for r in replies]
            changed[index]['id'] += 1
            with self.subTest(index=index), patch.object(api, 'api', side_effect=changed), self.assertRaises(ValueError):
                api.identity()

    def test_checkout_accepts_configured_repository_and_rejects_other_origin(self):
        b.CONFIG = b.Config(**dict(FIXTURE_CONFIG, repository='another/project.name'))
        for origin in ('git@github.com:another/project.name.git',
                       'https://github.com/another/project.name',
                       'ssh://git@github.com/another/project.name.git'):
            subprocess.run([b.CONFIG.git, '-C', str(self.cwd), 'remote', 'set-url', 'origin', origin], check=True)
            self.assertEqual(b.checkout(str(self.cwd))['branch'], 'codex/pilot')
        subprocess.run([b.CONFIG.git, '-C', str(self.cwd), 'remote', 'set-url', 'origin',
                        'https://github.com/another/wrong.git'], check=True)
        with self.assertRaisesRegex(ValueError, 'configured GitHub repository'):
            b.checkout(str(self.cwd))

    def test_repository_name_in_comment_url_is_literal(self):
        b.CONFIG = b.Config(**dict(FIXTURE_CONFIG, repository='example/project.name'))
        invalid = comment(101)
        invalid['issue_url'] = invalid['issue_url'].replace('project.name', 'projectXname')
        self.assertEqual(self.state.ingest([invalid]), [])
        self.assertEqual(len(self.state.ingest([comment(102)])), 1)

    def test_no_backlog_auth_and_exact_command(self):
        data = [comment(99), comment(100), comment(101, author=b.CONFIG.bot_user_id),
                comment(102, user={'id': b.CONFIG.owner_user_id, 'type': 'Bot'}),
                comment(103, '> /murphy start'), comment(104, 'hello /murphy start'),
                comment(105, item=227), comment(106, updated_at='2026-10-15T12:00:01Z'),
                comment(107, '/murphy merge abc'), comment(108, '/murphy start\nRead HEAD only.')]
        ids = self.state.ingest(data)
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.state.row(ids[0])['prompt'], 'Read HEAD only.')
        self.assertEqual(self.state.ingest(data), [])
        self.assertIsNone(b.command('/murphy start\nhello\x00'))
        self.assertIsNone(b.command('/murphy continue Read HEAD'))
        self.assertIsNone(b.command('/murphy continuex Read HEAD'))
        self.assertEqual(b.command(' /murphy start  \nRead HEAD'), ('start', 'Read HEAD'))
        self.assertIsNone(b.command('    /murphy start\nRead HEAD'))
        self.assertEqual(b.command('/murphy continue\r\nRead HEAD'), ('continue', 'Read HEAD'))

    def test_issue_and_pr_continue_same_thread(self):
        c1 = comment(101)
        c2 = comment(102, '/murphy continue', item=229)
        self.api.data = {101: c1, 102: c2}
        ids = self.state.ingest([c1, c2])
        self.worker.github = self.api
        self.execute()
        self.execute()
        first, second = map(self.state.row, ids)
        self.assertEqual(first['state'], 'succeeded')
        self.assertEqual(second['state'], 'succeeded')
        self.assertEqual(first['thread_id'], second['thread_id'])
        self.assertNotEqual(first['turn_id'], second['turn_id'])
        self.assertEqual(len(self.api.report_calls), 2)
        self.execute()
        self.assertEqual(len(self.api.report_calls), 2)

    def test_edited_or_deleted_comment_never_invokes_model(self):
        ident = self.state.ingest([comment(101)])[0]
        self.api.data[101] = comment(101, '/murphy start\nChanged')
        self.worker.github = self.api
        self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'cancelled')
        self.assertFalse(list(self.state.directory.glob('*.jsonl')))
        ident2 = self.state.ingest([comment(102)])[0]
        self.execute()
        self.assertEqual(self.state.row(ident2)['state'], 'cancelled')

    def test_global_fifo_between_sessions_waits_for_detached_descendant(self):
        first = self.state.enqueue('a', 'start', 'DESCENDANT')
        second = self.state.enqueue('b', 'start', 'read')
        self.execute()
        self.assertEqual(self.state.row(second)['state'], 'queued')
        self.execute()
        a, other = self.state.row(first), self.state.row(second)
        self.assertGreaterEqual(other['started'], a['finished'])
        elapsed = b.dt.datetime.fromisoformat(a['finished']) - b.dt.datetime.fromisoformat(a['started'])
        self.assertGreaterEqual(elapsed.total_seconds(), 0.9)

    def test_pause_and_cancel_without_model(self):
        ident = self.state.enqueue('a', 'start', 'read')
        self.state.control('pause')
        self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'queued')
        self.state.control('cancel', str(ident))
        self.state.control('resume')
        self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'cancelled')
        self.assertFalse(list(self.state.directory.glob('*.jsonl')))

    def test_global_lock_is_shared_by_independent_workers(self):
        code = (f'import sys;sys.path.insert(0,{str(ROOT.parent)!r});import bridge;'
                's=bridge.State(sys.argv[1]);\nwith bridge.lock(s): print("bad")')
        with b.lock(self.state):
            result = subprocess.run([sys.executable, '-c', code, str(self.state.directory)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already held', result.stderr)

    def test_global_lock_can_be_shared_across_state_directories(self):
        other = b.State(Path(self.temp.name) / 'another-state', self.state.worker_lock)
        self.addCleanup(other.db.close)
        with b.lock(self.state):
            with self.assertRaises(ValueError):
                with b.lock(other):
                    pass

    def test_restart_marks_unknown_and_never_replays(self):
        ident = self.state.enqueue('a', 'start', 'read')
        with self.state.db:
            self.state.db.execute("UPDATE commands SET state='running' WHERE id=?", (ident,))
        other = self.state.enqueue('b', 'start', 'read')
        self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'unknown')
        self.assertEqual(self.state.row(other)['state'], 'queued')
        self.assertEqual(self.state.setting('paused'), '1')
        with self.assertRaises(ValueError):
            self.state.control('resume')
        self.assertFalse(list(self.state.directory.glob('*.jsonl')))

    def test_no_completion_or_wrong_thread_blocks_next_command(self):
        ident = self.state.enqueue('a', 'start', 'NO_COMPLETION')
        other = self.state.enqueue('b', 'start', 'read')
        self.execute()
        self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'unknown')
        self.assertEqual(self.state.row(other)['state'], 'queued')

    def test_wrong_resume_thread_is_not_rebound(self):
        ident = self.state.enqueue('a', 'start', 'read')
        self.execute()
        original = self.state.row(ident)['thread_id']
        bad = self.state.enqueue('a', 'continue', 'WRONG_THREAD')
        self.execute()
        self.assertEqual(self.state.row(bad)['state'], 'unknown')
        self.assertEqual(self.state.db.execute("SELECT thread_id FROM tasks WHERE name='a'").fetchone()[0], original)

    def test_agent_edits_can_be_continued_without_committing(self):
        first = self.state.enqueue('a', 'start', 'WRITE_TASK_FILE')
        second = self.state.enqueue('a', 'continue', 'WRITE_TASK_FILE')
        self.execute()
        self.assertEqual(self.state.row(first)['state'], 'succeeded')
        self.execute()
        self.assertEqual(self.state.row(second)['state'], 'succeeded')
        self.assertEqual((self.cwd / 'task.txt').read_text(), 'task change\ntask change\n')
        self.assertEqual(self.state.row(first)['thread_id'], self.state.row(second)['thread_id'])
        self.assertEqual(self.state.setting('paused'), '0')

    def test_agent_commit_is_accepted_and_report_uses_new_head(self):
        before = b.checkout(str(self.cwd))['head']
        ident = self.state.enqueue('a', 'start', 'COMMIT_TASK_FILE')
        self.execute()
        row = self.state.row(ident)
        self.assertEqual(row['state'], 'succeeded')
        self.assertNotEqual(row['head'], before)
        self.assertEqual(row['head'], b.checkout(str(self.cwd))['head'])
        next_turn = self.state.enqueue('a', 'continue', 'COMMIT_TASK_FILE')
        self.execute()
        self.assertEqual(self.state.row(next_turn)['state'], 'succeeded')
        self.assertNotEqual(self.state.row(next_turn)['head'], row['head'])

    def test_agent_can_push_its_commit_to_a_local_bare_repository(self):
        remote = Path(self.temp.name) / 'remote.git'
        subprocess.run([b.CONFIG.git, 'init', '--bare', '-q', str(remote)], check=True)
        ident = self.state.enqueue('a', 'start', 'COMMIT_TASK_FILE PUSH_TASK_BRANCH')
        with patch.dict(os.environ, {'MURPHY_TEST_REMOTE': str(remote)}):
            self.execute()
        row = self.state.row(ident)
        self.assertEqual(row['state'], 'succeeded')
        pushed = subprocess.check_output([b.CONFIG.git, '--git-dir', str(remote), 'rev-parse',
                                         'refs/heads/codex/pilot'], text=True).strip()
        self.assertEqual(pushed, row['head'])

    def test_external_modification_of_tracked_dirty_contents_blocks_execution(self):
        self.state.enqueue('a', 'start', 'COMMIT_TASK_FILE')
        self.execute()
        self.state.enqueue('a', 'continue', 'WRITE_TASK_FILE')
        self.execute()
        (self.cwd / 'task.txt').write_text('external edit to already dirty tracked file')
        continued = self.state.enqueue('a', 'continue', 'read')
        self.execute()
        self.assertEqual(self.state.row(continued)['state'], 'unknown')
        self.assertEqual(len(list(self.state.directory.glob('*.jsonl'))), 2)

    def test_external_edit_to_an_agent_file_is_preserved_and_blocks_execution(self):
        self.state.enqueue('a', 'start', 'WRITE_TASK_FILE')
        self.execute()
        (self.cwd / 'task.txt').write_text('human edit; same untracked filename')
        continued = self.state.enqueue('a', 'continue', 'read')
        self.execute()
        self.assertEqual(self.state.row(continued)['state'], 'unknown')
        self.assertEqual((self.cwd / 'task.txt').read_text(), 'human edit; same untracked filename')
        self.assertEqual(len(list(self.state.directory.glob('*.jsonl'))), 1)

    def test_external_commit_after_a_turn_blocks_execution(self):
        self.state.enqueue('a', 'start', 'COMMIT_TASK_FILE')
        self.execute()
        subprocess.run([b.CONFIG.git, '-C', str(self.cwd), '-c', 'user.name=Test Human',
                        '-c', 'user.email=human@example.invalid', 'commit', '--allow-empty', '-qm', 'external'], check=True)
        continued = self.state.enqueue('a', 'continue', 'read')
        self.execute()
        self.assertEqual(self.state.row(continued)['state'], 'unknown')
        self.assertEqual(len(list(self.state.directory.glob('*.jsonl'))), 1)

    def test_agent_branch_switch_still_pauses_and_preserves_changes(self):
        ident = self.state.enqueue('a', 'start', 'WRITE_TASK_FILE SWITCH_TASK_BRANCH')
        self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'unknown')
        self.assertEqual(self.state.setting('paused'), '1')
        self.assertTrue((self.cwd / 'task.txt').exists())
        self.assertEqual(b.git(str(self.cwd), 'branch', '--show-current'), 'codex/unexpected')

    def test_writable_permission_arguments_and_prompt_apply_to_start_and_resume(self):
        first = self.state.enqueue('a', 'start', 'read')
        second = self.state.enqueue('a', 'continue', 'read')
        self.execute()
        self.execute()
        for ident in (first, second):
            row = self.state.row(ident)
            event = json.loads((self.state.directory / (row['execution_id'] + '.jsonl')).read_text().splitlines()[0])
            args = event['argv']
            self.assertIn('default_permissions="murphy"', args)
            self.assertIn('approval_policy="never"', args)
            self.assertNotIn('read-only', args)
            self.assertNotIn('danger-full-access', args)
            profile = next(arg for arg in args if arg.startswith('permissions.murphy='))
            self.assertIn('extends = ":workspace"', profile)
            self.assertIn('network = { enabled = true }', profile)
            self.assertIn(str(self.cwd / '.git'), profile)
            self.assertIn('commit, push the task branch', event['prompt'])
            self.assertIn('Configured GitHub CLI executable:', event['prompt'])

    def test_timeout_kills_detached_child_before_slot_is_released(self):
        self.worker.timeout = 0.3
        ident = self.state.enqueue('a', 'start', 'DAEMON_FOREVER')
        started = time.monotonic()
        self.execute()
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(self.state.row(ident)['state'], 'unknown')
        self.assertEqual(b.descendants(), {})

    def test_continue_without_binding_never_starts_a_new_session(self):
        ident = self.state.enqueue('a', 'continue', 'read')
        self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'failed')
        self.assertFalse(list(self.state.directory.glob('*.jsonl')))

    def test_repeated_start_is_rejected_without_replacing_or_pausing_session(self):
        first = self.state.enqueue('a', 'start', 'read')
        repeated = self.state.enqueue('a', 'start', 'read')
        continued = self.state.enqueue('a', 'continue', 'read')
        self.execute()
        self.execute()
        self.execute()
        self.assertEqual(self.state.row(repeated)['state'], 'failed')
        self.assertEqual(self.state.row(continued)['state'], 'succeeded')
        self.assertEqual(self.state.row(first)['thread_id'], self.state.row(continued)['thread_id'])
        self.assertEqual(self.state.setting('paused'), '0')
        self.assertEqual(len(list(self.state.directory.glob('*.jsonl'))), 2)

    def test_second_init_cannot_reset_activation(self):
        with self.assertRaises(ValueError):
            self.state.initialize(self.api)

    def test_idle_poll_does_not_spawn_codex(self):
        self.worker.github = self.api
        self.execute()
        self.execute()
        self.assertFalse(list(self.state.directory.glob('*.jsonl')))

    def worker_process(self):
        code = (f'import sys;sys.path.insert(0,{str(ROOT.parent)!r});import bridge;'
                f'bridge.configure(sys.argv[2]);s=bridge.State(sys.argv[1]);bridge.Worker(s,codex={FAKE!r},timeout=10).run(once=True)')
        return subprocess.Popen([sys.executable, '-c', code, str(self.state.directory), str(self.config_path)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def wait_running(self, ident):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            row = self.state.row(ident)
            if row['state'] == 'running' and row['thread_id']:
                return
            time.sleep(0.02)
        self.fail('Worker did not reach a live turn')

    def test_active_cancel_drains_all_descendants(self):
        ident = self.state.enqueue('a', 'start', 'DAEMON_FOREVER')
        process = self.worker_process()
        self.wait_running(ident)
        self.state.control('cancel', str(ident))
        _, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, error)
        self.assertEqual(self.state.row(ident)['state'], 'cancelled')
        with b.lock(self.state):
            pass

    def test_killed_worker_cannot_overlap_or_replay_live_child(self):
        ident = self.state.enqueue('a', 'start', 'SLOW')
        queued = self.state.enqueue('b', 'start', 'read')
        process = self.worker_process()
        self.wait_running(ident)
        process.kill()
        process.communicate(timeout=3)
        with self.assertRaises(ValueError):
            with b.lock(self.state):
                pass
        time.sleep(1.2)
        for pid, (status, _) in b.descendants().items():
            if status == 'Z':
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
        self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'unknown')
        self.assertEqual(self.state.row(queued)['state'], 'queued')
        self.assertEqual(len(list(self.state.directory.glob('*.jsonl'))), 1)

    def test_report_failure_never_reexecutes_command(self):
        ident = self.state.ingest([comment(101)])[0]
        self.api.data[101] = comment(101)
        def unavailable(row):
            raise RuntimeError('temporary report outage')
        self.api.report = unavailable
        self.worker.github = self.api
        self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'succeeded')
        self.assertIsNone(self.state.row(ident)['report_id'])
        self.api.report = lambda row: 1001
        self.worker.report_retry_after = 0
        self.execute()
        self.assertEqual(self.state.row(ident)['report_id'], 1001)
        self.assertEqual(len(list(self.state.directory.glob('*.jsonl'))), 1)

    def test_poll_failure_keeps_cursor_and_active_turn_can_finish(self):
        before = self.state.setting('since')
        def unavailable(since):
            raise RuntimeError('API unavailable')
        self.api.comments = unavailable
        self.worker.github = self.api
        self.worker.poll()
        self.assertEqual(self.state.setting('since'), before)
        ident = self.state.enqueue('a', 'start', 'read')
        self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'succeeded')

    def test_rollout_turn_lookup_requires_pilot_session_and_current_turn(self):
        thread = '11111111-1111-4111-8111-111111111111'
        root = Path(self.temp.name) / 'codex'
        directory = root / 'sessions/2026/10/15'
        directory.mkdir(parents=True)
        path = directory / ('rollout-' + thread + '.jsonl')
        task = {'codex_home': str(root), 'cwd': str(self.cwd)}
        records = [
            {'type': 'session_meta', 'payload': {'id': thread, 'source': 'exec', 'cwd': str(self.cwd)}},
            {'type': 'turn_context', 'timestamp': '2026-10-15T12:00:00Z', 'payload': {'turn_id': thread}}
        ]
        path.write_text('\n'.join(json.dumps(r) for r in records))
        self.assertEqual(b.rollout_turn(task, thread, '2026-10-15T11:59:59+00:00'), thread)
        self.assertIsNone(b.rollout_turn(task, thread, '2026-10-15T12:00:01+00:00'))
        self.assertIsNone(b.rollout_turn(task, thread, '2026-10-15T11:59:59+00:00', '2026-10-15T11:59:59.5+00:00'))
        task['cwd'] = '/wrong'
        self.assertIsNone(b.rollout_turn(task, thread, '2026-10-15T11:59:59+00:00'))

    def test_github_comment_pagination_is_flattened(self):
        result = subprocess.CompletedProcess([], 0, json.dumps([[comment(101)], [comment(102)]]), '')
        with patch.object(b.subprocess, 'run', return_value=result) as run:
            comments = b.GitHub().comments('2026-10-15T12:00:00Z')
        self.assertEqual([c['id'] for c in comments], [101, 102])
        self.assertIn('--paginate', run.call_args.args[0])
        self.assertIn('--slurp', run.call_args.args[0])

    def test_uncertain_report_post_reconciles_marker_without_duplicate(self):
        ident = self.state.enqueue('a', 'start', 'read', item=226)
        self.state.finish(ident, 'succeeded', 'read done')
        row = self.state.row(ident)
        existing = {'id': 1001, 'user': {'id': b.CONFIG.bot_user_id},
                    'body': '<!-- codex-bridge:' + row['execution_id'] + ' -->'}
        api = b.GitHub()
        with patch.object(api, 'api', side_effect=[[existing], {'id': 1001}]) as call:
            self.assertEqual(api.report(row), 1001)
        self.assertEqual(call.call_args_list[1].args[1], 'PATCH')
        self.assertEqual(call.call_count, 2)

    def setup_autostart(self, number=500, body='Reasoning: high\n\nRead HEAD and remember ACORN.'):
        subprocess.run([b.CONFIG.git, '-C', str(self.cwd), 'update-ref', 'refs/remotes/origin/main', 'HEAD'], check=True)
        self.state.configure_autostart(self.cwd, Path(self.temp.name) / 'automatic')
        self.api.issues[number] = {'number': number, 'title': 'A new issue', 'body': body,
                                   'updated_at': '2026-10-15T12:00:00Z'}
        self.worker.github = self.api
        original = b.git
        # Exercise real Git worktree creation; only network fetch is replaced by a fixture ref.
        return patch.object(b, 'git', side_effect=lambda cwd, *args: '' if args[0] == 'fetch' else original(cwd, *args))

    def test_enabling_autostart_does_not_execute_preexisting_unknown_commands(self):
        self.setup_autostart()
        with self.state.db:
            self.state.db.execute("DELETE FROM settings WHERE key='autostart_baseline'")
        self.state.configure_autostart(self.cwd, Path(self.temp.name) / 'automatic', baseline=110)
        self.assertEqual(self.state.ingest([comment(105, item=500)]), [])
        self.assertEqual(len(self.state.ingest([comment(111, item=500)])), 1)
        self.state.configure_autostart(self.cwd, Path(self.temp.name) / 'automatic', baseline=999)
        self.assertEqual(self.state.setting('autostart_baseline'), '110')

    def test_new_issue_start_creates_worktree_session_and_freezes_reasoning(self):
        fetch = self.setup_autostart(body='Reasoning: medium\n\nRead HEAD and remember ACORN.')
        self.api.data = {101: comment(101, '/murphy start\nRead this issue.', item=500),
                         102: comment(102, '/murphy continue\nRecall the issue context.', item=500)}
        first, second = self.state.ingest(list(self.api.data.values()))
        self.assertIsNone(self.state.row(first)['task'])
        self.assertFalse((Path(self.temp.name) / 'automatic').exists())
        with fetch:
            self.execute()
            self.api.issues[500]['body'] = 'Reasoning: low\nChanged after the start.'
            self.execute()
        task = self.state.db.execute("SELECT * FROM tasks WHERE name='issue-500'").fetchone()
        self.assertEqual(task['branch'], 'codex/issue-500-murphy')
        self.assertEqual(task['reasoning'], 'medium')
        self.assertEqual(task['cwd'], str(Path(self.temp.name) / 'automatic/issue-500'))
        self.assertEqual(b.git(str(self.cwd), 'branch', '--show-current'), 'codex/pilot')
        self.assertEqual(self.state.row(first)['state'], 'succeeded')
        self.assertEqual(self.state.row(second)['state'], 'succeeded')
        self.assertEqual(self.state.row(first)['thread_id'], self.state.row(second)['thread_id'])
        for ident in (first, second):
            row = self.state.row(ident)
            event = json.loads((self.state.directory / (row['execution_id'] + '.jsonl')).read_text().splitlines()[0])
            self.assertIn('model_reasoning_effort="medium"', event['argv'])
            if ident == first:
                self.assertIn('ACORN', event['prompt'])
            else:
                self.assertNotIn('Changed after the start.', event['prompt'])
        self.assertEqual(self.state.ingest(list(self.api.data.values())), [])

    def test_autostart_without_reasoning_pins_high_for_start_and_continue(self):
        fetch = self.setup_autostart(body='Read HEAD only.')
        self.api.data = {101: comment(101, item=500),
                         102: comment(102, '/murphy continue', item=500)}
        ids = self.state.ingest(list(self.api.data.values()))
        with fetch:
            self.execute()
            self.execute()
        for ident in ids:
            row = self.state.row(ident)
            self.assertEqual(row['state'], 'succeeded')
            self.assertEqual(row['reasoning'], 'high')
            event = json.loads((self.state.directory / (row['execution_id'] + '.jsonl')).read_text().splitlines()[0])
            self.assertIn('model_reasoning_effort="high"', event['argv'])
        task = self.state.db.execute("SELECT reasoning FROM tasks WHERE name='issue-500'").fetchone()
        self.assertEqual(task['reasoning'], 'high')

    def test_reasoning_metadata_is_literal_and_unambiguous(self):
        self.assertEqual(b.issue_reasoning('Reasoning: high'), 'high')
        self.assertEqual(b.issue_reasoning('Reasoning: XHIGH\r\nText'), 'xhigh')
        self.assertIsNone(b.issue_reasoning('```text\nReasoning: high\n```\n> Reasoning: low\n    Reasoning: max'))
        for body in ('Reasoning: expensive', 'Reasoning: high\nReasoning: high', 'Reasoning:'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                b.issue_reasoning(body)

    def test_invalid_reasoning_stops_before_git_or_model(self):
        fetch = self.setup_autostart(body='Reasoning: made-up')
        self.api.data[101] = comment(101, item=500)
        ident = self.state.ingest(list(self.api.data.values()))[0]
        with fetch as git_mock:
            self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'failed')
        self.assertIn('Invalid Reasoning', self.state.row(ident)['detail'])
        self.assertFalse((Path(self.temp.name) / 'automatic').exists())
        self.assertFalse(list(self.state.directory.glob('*.jsonl')))
        self.assertFalse(any(call.args[1] == 'fetch' for call in git_mock.call_args_list))

    def test_unbound_continue_never_bootstraps_a_session(self):
        fetch = self.setup_autostart()
        self.api.data[101] = comment(101, '/murphy continue', item=500)
        ident = self.state.ingest(list(self.api.data.values()))[0]
        with fetch:
            self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'failed')
        self.assertFalse((Path(self.temp.name) / 'automatic').exists())
        self.assertFalse(list(self.state.directory.glob('*.jsonl')))

    def test_autostart_never_overwrites_an_existing_directory(self):
        fetch = self.setup_autostart()
        directory = Path(self.temp.name) / 'automatic/issue-500'
        directory.mkdir(parents=True)
        (directory / 'human.txt').write_text('preserve')
        self.api.data[101] = comment(101, item=500)
        ident = self.state.ingest(list(self.api.data.values()))[0]
        with fetch:
            self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'failed')
        self.assertEqual((directory / 'human.txt').read_text(), 'preserve')
        self.assertFalse(list(self.state.directory.glob('*.jsonl')))

    def test_autostart_never_replaces_an_existing_issue_branch(self):
        fetch = self.setup_autostart()
        subprocess.run([b.CONFIG.git, '-C', str(self.cwd), 'branch', 'codex/issue-500-existing'], check=True)
        self.api.data[101] = comment(101, item=500)
        ident = self.state.ingest(list(self.api.data.values()))[0]
        with fetch:
            self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'failed')
        self.assertIn('already exists', self.state.row(ident)['detail'])
        self.assertFalse(list(self.state.directory.glob('*.jsonl')))

    def test_failure_after_worktree_creation_preserves_plan_and_pauses(self):
        fetch = self.setup_autostart()
        self.api.data[101] = comment(101, item=500)
        ident = self.state.ingest(list(self.api.data.values()))[0]
        with fetch, patch.object(self.state, 'register', side_effect=RuntimeError('process stopped after creating worktree')):
            self.execute()
        row = self.state.row(ident)
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(json.loads(row['plan'])['reasoning'], 'high')
        self.assertTrue(Path(json.loads(row['plan'])['cwd']).exists())
        self.assertEqual(self.state.setting('paused'), '1')
        self.assertFalse(list(self.state.directory.glob('*.jsonl')))
        self.execute()
        self.assertEqual(self.state.row(ident)['state'], 'unknown')

    def test_parent_feature_branch_is_selected_only_when_unambiguous(self):
        api = b.GitHub()
        pull = {'head': {'ref': 'codex/issue-499-feature', 'repo': {'id': b.CONFIG.repository_id}}}
        with patch.object(api, 'api', side_effect=[{'number': 499}, [pull]]):
            self.assertEqual(api.start_base(500, 'main'), 'codex/issue-499-feature')
        with patch.object(api, 'api', side_effect=[None]):
            self.assertEqual(api.start_base(500, 'main'), 'main')
        other = {'head': {'ref': 'codex/issue-499-other', 'repo': {'id': b.CONFIG.repository_id}}}
        with patch.object(api, 'api', side_effect=[{'number': 499}, [pull, other]]), self.assertRaises(ValueError):
            api.start_base(500, 'main')

    def test_closed_issue_or_pr_cannot_be_automatic_start_target(self):
        api = b.GitHub()
        source = {'number': 500, 'url': f'https://api.github.com/repos/{b.CONFIG.repository}/issues/500', 'state': 'closed'}
        with patch.object(api, 'api', return_value=source), self.assertRaises(ValueError):
            api.issue(500)
        source.update(state='open', pull_request={})
        with patch.object(api, 'api', return_value=source), self.assertRaises(ValueError):
            api.issue(500)

    def test_register_with_explicit_reasoning_survives_restart(self):
        self.state.register('explicit', self.cwd, 'codex/pilot', reasoning='xhigh')
        another = b.State(self.state.directory)
        self.addCleanup(another.db.close)
        self.assertEqual(another.db.execute("SELECT reasoning FROM tasks WHERE name='explicit'").fetchone()[0], 'xhigh')

    def test_high_default_applies_to_new_sessions_and_preserves_legacy_session(self):
        self.state.enqueue('b', 'start', 'read')
        self.execute()
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET reasoning=NULL')
        first = self.state.enqueue('a', 'start', 'read')
        second = self.state.enqueue('b', 'continue', 'read')
        self.execute()
        self.execute()
        for ident, effort in ((first, 'high'), (second, None)):
            row = self.state.row(ident)
            self.assertEqual(row['state'], 'succeeded')
            self.assertEqual(row['reasoning'], effort)
            task = self.state.db.execute('SELECT reasoning FROM tasks WHERE name=?', (row['task'],)).fetchone()
            self.assertEqual(task['reasoning'], effort)
            event = json.loads((self.state.directory / (row['execution_id'] + '.jsonl')).read_text().splitlines()[0])
            if effort:
                self.assertIn('model_reasoning_effort="high"', event['argv'])
            else:
                self.assertFalse(any(arg.startswith('model_reasoning_effort=') for arg in event['argv']))

    def test_legacy_task_without_checkpoint_can_start_clean_but_not_dirty(self):
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET checkout_json=NULL')
        started = self.state.enqueue('a', 'start', 'read')
        self.execute()
        self.assertEqual(self.state.row(started)['state'], 'succeeded')
        self.assertIsNotNone(self.state.db.execute("SELECT checkout_json FROM tasks WHERE name='a'").fetchone()[0])
        (self.cwd / 'human.txt').write_text('preserve')
        other = self.state.enqueue('b', 'start', 'read')
        self.execute()
        self.assertEqual(self.state.row(other)['state'], 'unknown')

    def test_new_worker_restores_pending_queue(self):
        ident = self.state.enqueue('a', 'start', 'read')
        another = b.State(self.state.directory)
        self.addCleanup(another.db.close)
        b.Worker(another, codex=FAKE).run(once=True)
        self.assertEqual(self.state.row(ident)['state'], 'succeeded')


if __name__ == '__main__':
    unittest.main(verbosity=2)
