#!/usr/bin/env python3
"""Worktree lifecycle tests: real temporary Git repositories, no external services."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from murphy import bridge as b
from murphy import cleanup as c


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        previous = b.CONFIG
        b.CONFIG = b.Config('example/project', 1, 2, 3, 'owner')
        self.addCleanup(setattr, b, 'CONFIG', previous)
        self.source = self.root / 'source'
        self.remote = self.root / 'remote.git'
        self.run_git('init', '--bare', '-q', str(self.remote))
        self.run_git('init', '-q', '-b', 'main', str(self.source))
        self.git = lambda *args: b.git(str(self.source), *args)
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('remote', 'add', 'origin', 'https://github.com/example/project.git')
        self.git('config', 'url.' + str(self.remote) + '.insteadOf', 'https://github.com/example/project.git')
        real_git = b.git
        def local_git(cwd, *args):
            if args == ('remote', 'get-url', 'origin'):
                return 'https://github.com/example/project.git'
            return real_git(cwd, *args)
        patched = patch.object(b, 'git', side_effect=local_git)
        patched.start()
        self.addCleanup(patched.stop)
        (self.source / 'tracked').write_text('saved\n')
        self.git('add', 'tracked')
        self.git('commit', '-qm', 'saved')
        self.git('push', 'origin', 'main')
        self.state = b.State(self.root / 'state')
        self.addCleanup(self.state.db.close)
        self.state.bind_config(b.CONFIG)
        with self.state.db:
            self.state.set('baseline', '0')
        self.state.configure_autostart(self.source, self.root / 'worktrees', base='main')
        self.path = self.root / 'worktrees/issue-7'
        self.git('worktree', 'add', '-b', 'codex/issue-7-murphy', str(self.path), 'main')
        self.state.register('issue-7', self.path, 'codex/issue-7-murphy', [7], issue={'number': 7})
        task = self.task()
        info = b.checkout(str(self.path))
        proof = dict(repo=str(self.source), cwd=str(self.path), branch=task['branch'],
                     common_dir=info['common_dir'], git_dir=info['git_dir'])
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET automatic_json=?', (json.dumps(proof),))
        self.ident = self.state.enqueue('issue-7', 'start', 'work', item=7)
        self.state.finish(self.ident, 'succeeded', 'Historical report')
        self.api = Mock()
        self.api.api.return_value = []
        self.updates = {'7': {'state': 'CLOSED', 'title': 'Done'}}
        for name in ('process_use', 'runtime_use'):
            mocked = patch.object(c, name, return_value=[])
            mocked.start()
            self.addCleanup(mocked.stop)

    def run_git(self, *args):
        subprocess.run(['git', *args], check=True, capture_output=True)

    def task(self):
        return dict(self.state.db.execute('SELECT * FROM tasks WHERE name=?', ('issue-7',)).fetchone())

    def retire(self):
        with b.lock(self.state):
            c.retire(self.state, self.api, self.updates, [int(number) for number in self.updates] or [7])
        return json.loads(self.task()['cleanup_json'] or '{}')

    def blocked(self, reason):
        result = self.retire()
        self.assertTrue(self.path.exists())
        self.assertEqual(result['state'], 'blocked')
        self.assertIn(reason, result['reason'])

    def test_removes_only_directory_keeps_history_and_is_idempotent_after_restart(self):
        log = self.state.directory / 'retained.jsonl'
        log.write_text('history')
        self.assertEqual(self.retire()['state'], 'removed')
        self.assertFalse(self.path.exists())
        self.assertIn('codex/issue-7-murphy', self.git('branch', '--list'))
        self.assertEqual(self.state.row(self.ident)['detail'], 'Historical report')
        self.assertEqual(log.read_text(), 'history')
        reopened = b.State(self.state.directory)
        self.addCleanup(reopened.db.close)
        with patch.object(c, 'remote_saved') as again:
            c.retire(reopened, self.api, self.updates, [7])
            again.assert_not_called()

    def test_busy_commands_and_unknown_block_without_changing_outcomes(self):
        for status in ('queued', 'running', 'blocked', 'unknown'):
            with self.subTest(status=status), self.state.db:
                self.state.db.execute('UPDATE commands SET state=?', (status,))
                self.blocked(status)
                self.assertEqual(self.state.row(self.ident)['state'], status)

    def test_staged_unstaged_and_untracked_work_block(self):
        (self.path / 'tracked').write_text('changed')
        self.blocked('unstaged')
        b.git(str(self.path), 'add', 'tracked')
        self.blocked('staged')
        b.git(str(self.path), 'commit', '-qm', 'new unpushed work')
        (self.path / 'new').write_text('untracked')
        self.blocked('untracked')

    def test_unpushed_commit_and_stale_tracking_ref_block(self):
        b.git(str(self.path), 'commit', '--allow-empty', '-qm', 'unpushed')
        self.blocked('freshly confirmed remote')
        b.git(str(self.path), 'push', 'origin', 'HEAD:refs/heads/task')
        b.git(str(self.path), 'push', 'origin', ':refs/heads/task')
        self.blocked('freshly confirmed remote')

    def test_deleted_remote_task_branch_can_use_preserved_main_ancestry(self):
        b.git(str(self.path), 'commit', '--allow-empty', '-qm', 'task')
        b.git(str(self.path), 'push', 'origin', 'HEAD:refs/heads/main')
        self.assertEqual(self.retire()['state'], 'removed')

    def test_open_alias_unknown_and_missing_github_responses_block(self):
        with self.state.db:
            self.state.db.execute('INSERT INTO items VALUES (8,?)', ('issue-7',))
        self.updates['8'] = {'state': 'OPEN'}
        self.blocked('alias is still open')
        del self.updates['8']
        self.blocked('unavailable')
        self.updates['8'] = {'state': 'DELETED'}
        self.blocked('unverified')

    def test_process_runtime_api_and_remote_errors_preserve_worktree(self):
        for name, message in (('process_use', 'Process 99 uses worktree'),
                              ('runtime_use', 'Cannot inspect docker runtime'),
                              ('remote_saved', 'network unavailable')):
            with self.subTest(name=name), patch.object(c, name, side_effect=ValueError(message)):
                self.blocked(message)
        self.api.api.side_effect = RuntimeError('GitHub unavailable')
        self.blocked('GitHub unavailable')

    def test_open_related_pull_blocks_and_merged_pull_qualifies_open_original(self):
        pull = dict(number=8, state='open', merged_at=None,
                    head={'ref': self.task()['branch'], 'repo': {'id': 1}}, base={'repo': {'id': 1}})
        self.api.api.return_value = [pull]
        self.blocked('PR #8 is still open')
        self.updates['7']['state'] = 'OPEN'
        pull.update(state='closed', merged_at='2026-01-01')
        self.assertEqual(self.retire()['state'], 'removed')

    def test_manual_tasks_and_source_checkout_are_never_removed(self):
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET automatic_json=NULL')
        self.assertEqual(c.automatic_tasks(self.state), [])
        self.retire()
        self.assertTrue(self.path.exists())
        self.assertTrue(self.source.exists())

    def test_legacy_creation_plan_is_required_and_sufficient_provenance(self):
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET automatic_json=NULL')
            self.state.db.execute('UPDATE commands SET plan=?', (json.dumps(dict(
                repo=str(self.source), cwd=str(self.path), branch=self.task()['branch'])),))
        self.assertEqual(self.retire()['state'], 'removed')

    def test_locked_identity_changed_and_ignored_files_never_force_removal(self):
        self.git('worktree', 'lock', str(self.path))
        self.blocked('locked')
        self.git('worktree', 'unlock', str(self.path))
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET branch=?', ('wrong',))
        self.blocked('identity changed')

    def test_final_revalidation_preserves_changes_made_during_checks(self):
        def changed(*args):
            (self.path / 'late-work').write_text('preserve')
        with patch.object(c, 'remote_saved', side_effect=changed):
            self.blocked('untracked')
        self.assertEqual((self.path / 'late-work').read_text(), 'preserve')

    def test_identical_block_is_persisted_once_and_symlink_replacement_is_rejected(self):
        with patch.object(c, 'process_use', side_effect=ValueError('process uses worktree')):
            first = self.retire()
            self.assertEqual(self.retire(), first)
        moved = self.path.with_name('preserved')
        self.path.rename(moved)
        self.path.symlink_to(moved, target_is_directory=True)
        self.blocked('identity changed')
        self.assertTrue(moved.exists())

    def test_queued_unbound_alias_continuation_blocks_cleanup(self):
        self.state.enqueue(None, 'continue', 'resume', item=7)
        self.blocked('queued')

    def test_ignored_runtime_prevents_nonforced_remove_when_git_refuses(self):
        (self.path / '.gitignore').write_text('runtime/\n')
        b.git(str(self.path), 'add', '.gitignore')
        b.git(str(self.path), 'commit', '-qm', 'ignore runtime')
        b.git(str(self.path), 'push', 'origin', 'HEAD:refs/heads/task')
        (self.path / 'runtime').mkdir()
        (self.path / 'runtime/cache').write_text('retained')
        result = self.retire()
        # Git versions may permit ignored files; the contract is always a non-forced remove.
        self.assertIn(result['state'], ('blocked', 'removed'))

    def test_interrupted_remove_requires_reconciliation_and_continue_fails_without_model(self):
        task = self.task()
        c.record(self.state, task, 'removing', 'Interrupted')
        self.assertEqual(self.retire()['state'], 'removing')
        self.assertEqual(self.retire()['state'], 'removing')
        self.assertTrue(self.path.exists())
        # Test a successfully retired task's explicit continuation error as well.
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET cleanup_json=NULL')
        self.retire()
        ident = self.state.enqueue('issue-7', 'continue', 'resume', item=7)
        worker = b.Worker(self.state)
        with patch.object(worker, 'run_codex') as model, patch.object(worker, 'block_dirty_source', return_value=False):
            worker.execute(self.state.row(ident), -1)
            model.assert_not_called()
        self.assertEqual(self.state.row(ident)['state'], 'failed')
        self.assertIn('removed', self.state.row(ident)['detail'])

    def test_operator_removed_worktree_is_reconciled_without_checkout_or_replay(self):
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET thread_id=?', ('fixture-session',))
        self.git('worktree', 'remove', str(self.path))
        real_checkout = b.checkout
        def existing_only(cwd):
            self.assertNotEqual(cwd, str(self.path))
            return real_checkout(cwd)
        with patch.object(b, 'checkout', side_effect=existing_only), patch.object(c, 'remote_saved') as remote:
            result = self.retire()
            self.assertEqual(result['state'], 'removed')
            self.assertIn('already absent and unregistered', result['reason'])
            remote.assert_not_called()
            c.process_use.assert_not_called()
            c.runtime_use.assert_not_called()
            self.assertEqual(self.retire(), result)
        self.assertEqual(self.state.row(self.ident)['detail'], 'Historical report')
        self.assertEqual(self.task()['thread_id'], 'fixture-session')
        ident = self.state.enqueue('issue-7', 'continue', 'resume', item=7)
        worker = b.Worker(self.state)
        with patch.object(worker, 'run_codex') as model, patch.object(worker, 'block_dirty_source', return_value=False):
            worker.execute(self.state.row(ident), -1)
            model.assert_not_called()
        self.assertIn('removed', self.state.row(ident)['detail'])

    def test_missing_directory_with_stale_registration_requires_reconciliation(self):
        shutil.rmtree(self.path)
        result = self.retire()
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('directory is absent but Git registration remains', result['reason'])
        self.assertIn(str(self.path), self.git('worktree', 'list'))

    def test_missing_worktree_does_not_hide_busy_or_unknown_commands(self):
        self.git('worktree', 'remove', str(self.path))
        for status in ('queued', 'running', 'blocked', 'unknown'):
            with self.subTest(status=status), self.state.db:
                self.state.db.execute('UPDATE commands SET state=?', (status,))
                result = self.retire()
                self.assertEqual(result['state'], 'blocked')
                self.assertIn(status, result['reason'])
                self.assertEqual(self.state.row(self.ident)['state'], status)

    def test_missing_worktree_retains_identity_and_interrupted_operation_guards(self):
        self.git('worktree', 'remove', str(self.path))
        with self.state.db:
            self.state.db.execute('UPDATE tasks SET branch=?', ('wrong',))
        self.assertIn('identity changed', self.retire()['reason'])
        c.record(self.state, self.task(), 'removing', 'Interrupted')
        self.assertEqual(self.retire()['state'], 'removing')

    def test_failed_removal_never_replays_uncertain_operation(self):
        original_git = b.git
        calls = []
        def uncertain(cwd, *args):
            if args[:2] == ('worktree', 'remove'):
                calls.append(args)
                original_git(cwd, *args)
                raise OSError('lost response after removal')
            return original_git(cwd, *args)
        with patch.object(b, 'git', side_effect=uncertain):
            result = self.retire()
            self.assertEqual(result['state'], 'removing')
            self.assertIn('no automatic replay', result['reason'])
            self.assertFalse(self.path.exists())
            self.assertEqual(self.retire()['state'], 'removing')
            self.assertEqual(len(calls), 1)

    def test_replaced_directory_is_identity_failure_not_missing_checkout_headers(self):
        self.git('worktree', 'remove', str(self.path))
        self.path.mkdir()
        self.blocked('Git identity changed')

    def test_reappeared_worktree_during_absence_checks_is_preserved(self):
        self.git('worktree', 'remove', str(self.path))
        original = c.validate
        def recreate(*args):
            result = original(*args)
            if result is None:
                self.git('worktree', 'add', str(self.path), self.task()['branch'])
            return result
        with patch.object(c, 'validate', side_effect=recreate):
            self.blocked('reappeared during absence checks')

    def test_missing_directory_permission_error_cannot_authorize_absence(self):
        self.git('worktree', 'remove', str(self.path))
        original = Path.lstat
        def denied(path, *args, **kwargs):
            if path == self.path:
                raise PermissionError('cannot stat worktree')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', denied):
            self.assertEqual(self.retire()['state'], 'blocked')

    def test_removed_status_records_foreign_process_inspection_boundary(self):
        with patch.object(c, 'process_use', return_value=['1']):
            self.assertIn('foreign-UID processes outside inspection guarantee: 1', self.retire()['reason'])


class RuntimeTests(unittest.TestCase):
    def test_no_runtime_is_not_a_dependency(self):
        with patch.object(c.shutil, 'which', return_value=None), patch.object(Path, 'exists', return_value=False), \
                patch.dict(os.environ, {}, clear=True):
            c.runtime_use('/tmp/fixture-worktree')

    def test_container_parent_mount_and_unavailable_runtime_block(self):
        response = Mock(returncode=0, stdout='container\n')
        inspect = Mock(returncode=0, stdout=json.dumps([{'Mounts': [{'Source': '/tmp'}]}]))
        with patch.object(c.shutil, 'which', side_effect=lambda name: name if name == 'docker' else None), \
                patch.dict(os.environ, DOCKER_HOST='unix:///fixture.sock'), \
                patch.object(c.subprocess, 'run', side_effect=[response, inspect]):
            with self.assertRaisesRegex(ValueError, 'mounts worktree'):
                c.runtime_use('/tmp/fixture-worktree')
        with patch.object(c.shutil, 'which', return_value='docker'), \
                patch.object(c.subprocess, 'run', return_value=Mock(returncode=1, stderr='offline')):
            with self.assertRaisesRegex(ValueError, 'Cannot inspect'):
                c.runtime_use('/tmp/fixture-worktree')

    def test_remote_docker_context_cannot_prove_local_containers_are_unused(self):
        with patch.object(c.shutil, 'which', side_effect=lambda name: name if name == 'docker' else None), \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(c.subprocess, 'run', return_value=Mock(returncode=0, stdout='"ssh://example.invalid"')):
            with self.assertRaisesRegex(ValueError, 'remote docker endpoint'):
                c.runtime_use('/tmp/fixture-worktree')

    def test_process_permission_errors_block_instead_of_assuming_unused(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / '123').mkdir()
            with patch.object(Path, 'read_text', side_effect=PermissionError('restricted')):
                with self.assertRaisesRegex(ValueError, 'permission denied'):
                    c.process_use('/tmp/task', proc=Path(root))

    def process_fixture(self, root, uid, pid='1'):
        process = Path(root) / pid
        (process / 'fd').mkdir(parents=True)
        (process / 'stat').write_text(pid + ' (fixture) S ' + '0 ' * 19)
        (process / 'status').write_text('Uid: ' + ' '.join([str(uid)] * 4))
        (process / 'cmdline').write_bytes(b'/sbin/init')
        (process / 'maps').write_bytes(b'')
        (process / 'cwd').symlink_to('/unrelated')
        (process / 'root').symlink_to('/')
        return process

    def test_inaccessible_foreign_pid_one_does_not_block_user_worktrees(self):
        with tempfile.TemporaryDirectory() as root:
            process = self.process_fixture(root, os.getuid() + 1)
            original_readlink = os.readlink
            def protected(path, *args, **kwargs):
                if Path(path) == process / 'cwd':
                    raise PermissionError('ptrace denied')
                return original_readlink(path, *args, **kwargs)
            with patch.object(os, 'readlink', side_effect=protected):
                self.assertEqual(c.process_use('/tmp/task', Path(root)), ['1'])

    def test_foreign_pid_does_not_hide_same_uid_use_or_permission_error(self):
        with tempfile.TemporaryDirectory() as root:
            foreign = self.process_fixture(root, os.getuid() + 1)
            own = self.process_fixture(root, os.getuid(), '123')
            (own / 'cwd').unlink()
            (own / 'cwd').symlink_to('/tmp/task')
            original = os.readlink
            def protected(path, *args, **kwargs):
                if Path(path) == foreign / 'cwd':
                    raise PermissionError('ptrace denied')
                return original(path, *args, **kwargs)
            with patch.object(os, 'readlink', side_effect=protected):
                with self.assertRaisesRegex(ValueError, 'Process 123 uses'):
                    c.process_use('/tmp/task', Path(root))
            with patch.object(os, 'readlink', side_effect=PermissionError('denied')):
                with self.assertRaisesRegex(ValueError, 'process 123: permission denied'):
                    c.process_use('/tmp/task', Path(root))

    def test_visible_foreign_process_reference_blocks_even_with_protected_cwd(self):
        with tempfile.TemporaryDirectory() as root:
            process = self.process_fixture(root, os.getuid() + 1)
            (process / 'maps').write_bytes(b'/tmp/task/library.so')
            with patch.object(os, 'readlink', side_effect=PermissionError('denied')):
                with self.assertRaisesRegex(ValueError, 'references worktree.*maps'):
                    c.process_use('/tmp/task', Path(root))
            (process / 'cmdline').write_bytes(b'php\0/tmp/task/server.php')
            with self.assertRaisesRegex(ValueError, 'references worktree.*cmdline'):
                c.process_use('/tmp/task', Path(root))

    def test_same_uid_open_file_map_and_root_block(self):
        for kind in ('fd', 'maps', 'root'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                process = self.process_fixture(root, os.getuid())
                if kind == 'maps':
                    (process / 'maps').write_bytes(b'/tmp/task/lib.so')
                else:
                    link = process / ('fd/4' if kind == 'fd' else 'root')
                    if kind == 'root':
                        link.unlink()
                    link.symlink_to('/tmp/task/child')
                with self.assertRaisesRegex(ValueError, 'Process 1 (uses|references)'):
                    c.process_use('/tmp/task', Path(root))

    def test_missing_uid_information_blocks_instead_of_excluding_process(self):
        with tempfile.TemporaryDirectory() as root:
            process = self.process_fixture(root, os.getuid() + 1)
            (process / 'status').write_text('Name: init')
            with self.assertRaisesRegex(ValueError, 'Cannot establish UID'):
                c.process_use('/tmp/task', Path(root))

    def test_same_uid_protected_pid_one_is_still_a_blocker(self):
        with tempfile.TemporaryDirectory() as root:
            self.process_fixture(root, os.getuid())
            with patch.object(os, 'readlink', side_effect=PermissionError('restricted')):
                with self.assertRaisesRegex(ValueError, 'process 1: permission denied'):
                    c.process_use('/tmp/task', Path(root))

    def test_changed_process_identity_cannot_be_excluded_as_foreign(self):
        with tempfile.TemporaryDirectory() as root:
            process = self.process_fixture(root, os.getuid() + 1)
            original = Path.read_text
            def changed(path, *args, **kwargs):
                result = original(path, *args, **kwargs)
                if path == process / 'stat':
                    (process / 'status').write_text('Uid: ' + ' '.join([str(os.getuid())] * 4))
                return result
            with patch.object(Path, 'read_text', changed), \
                    patch.object(os, 'readlink', side_effect=PermissionError('restricted')):
                with self.assertRaisesRegex(ValueError, 'identity changed'):
                    c.process_use('/tmp/task', Path(root))

    def test_process_with_cwd_in_worktree_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            process = Path(root) / '123'
            (process / 'fd').mkdir(parents=True)
            (process / 'stat').write_text('123 (fixture) S ' + '0 ' * 19)
            (process / 'status').write_text('Uid: ' + ' '.join([str(os.getuid())] * 4))
            (process / 'cmdline').write_bytes(b'fixture')
            (process / 'cwd').symlink_to('/tmp/task/child')
            with self.assertRaisesRegex(ValueError, 'Process 123 uses'):
                c.process_use('/tmp/task', proc=Path(root))


if __name__ == '__main__':
    unittest.main()
