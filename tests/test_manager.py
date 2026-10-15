#!/usr/bin/env python3
"""Multi-project behavior with temporary Git repositories and fake external APIs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from murphy import bridge as b
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
        fetch = patch.object(b, 'git', side_effect=lambda cwd, *args:
                             '' if args[0] == 'fetch' else original(cwd, *args))
        fetch.start()
        self.addCleanup(fetch.stop)
        self.manager.init(gh=sys.executable, codex=FAKE)
        self.previous = b.CONFIG
        self.addCleanup(setattr, b, 'CONFIG', self.previous)

    def account_api(self, account, path, pages=False):
        if path == 'user':
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
            return dict(self.user)
        if path == 'users/' + self.user['login']:
            return dict(self.user)
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
        if path == prefix + '/7/parent':
            return None
        if path == prefix + '/7':
            return {'number': 7, 'title': 'Task', 'body': 'Implement this issue.', 'state': 'open',
                    'updated_at': b.now(), 'url': 'https://api.github.com/' + path}
        if path.startswith(prefix + '/7/comments'):
            if method == 'POST':
                self.reported.append((repository, payload))
                return {'id': 9000 + len(self.reported)}
            return []
        raise AssertionError((path, method))

    def add_project(self, name, number):
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
        self.manager.add(repository, source)
        return self.manager.projects[number]

    def comment(self, repository, ident, prompt='read', verb='start', author=3001):
        comment = {'id': ident, 'body': f'/murphy {verb}\n{prompt}',
                   'issue_url': 'https://api.github.com/repos/' + repository + '/issues/7',
                   'created_at': '2026-10-15T12:00:00Z', 'updated_at': '2026-10-15T12:00:00Z',
                   'user': {'id': author, 'type': 'User'}}
        self.comments[repository].append(comment)
        for _, _, worker in self.manager.projects.values():
            worker.last_poll = 0
        return comment

    def tick(self):
        b.subreaper()
        with b.lock(self.manager) as fd:
            self.manager.tick(fd)

    def test_automatic_identity_and_same_account_configuration(self):
        config, state, _ = self.add_project('one', 1)
        self.assertEqual(config.owner_user_id, 3001)
        self.assertEqual(config.bot_user_id, config.owner_user_id)
        self.assertEqual(state.setting('baseline'), '0')
        with self.assertRaisesRegex(ValueError, 'Already initialized'):
            self.manager.init()
        with self.assertRaisesRegex(ValueError, 'already connected'):
            self.manager.add('example/one', self.root / 'one')

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
        self.assertTrue(any('Command 1:' in payload['body'] for _, payload in self.reported))
        self.assertTrue(any('Command 2:' in payload['body'] for _, payload in self.reported))
        self.comment('example/two', 103, 'WRITE_TASK_FILE', 'continue')
        self.tick()
        self.assertEqual(second.row(2)['thread_id'], second.row(1)['thread_id'])
        self.assertEqual(second.row(2)['state'], 'succeeded')
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
        self.manager.pump()
        self.user['id'] = 4001
        self.manager.last_identity_check = 0
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
            self.manager.last_identity_check = 0
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
            self.manager.last_identity_check = 0
            with patch.object(self.manager.account, 'verify', side_effect=RuntimeError('temporary API outage')):
                original()
        worker.poll = outage
        self.tick()
        self.assertEqual(state.row(1)['state'], 'succeeded')

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

    def test_available_projects_and_missing_prerequisites(self):
        self.add_project('one', 1)
        self.assertEqual(self.manager.available(), [{'repository': 'example/one', 'can_push': True}])
        from murphy.manager import prerequisite
        with self.assertRaisesRegex(ValueError, 'Missing executable'):
            prerequisite('/missing/example/tool')


if __name__ == '__main__':
    unittest.main(verbosity=2)
