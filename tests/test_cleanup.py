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
        inventory = patch.object(c, 'container_plan', return_value=[])
        self.container_plan = inventory.start()
        self.addCleanup(inventory.stop)

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
        self.api.reset_mock()
        c.retire(reopened, self.api, self.updates, [7])
        self.api.api.assert_not_called()

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

    def test_closed_task_removes_worktree_and_preserves_unpushed_commits_on_branch(self):
        b.git(str(self.path), 'commit', '--allow-empty', '-qm', 'unpushed')
        head = b.git(str(self.path), 'rev-parse', 'HEAD')
        self.assertEqual(self.retire()['state'], 'removed')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.git('rev-parse', self.task()['branch']), head)

    def test_squash_merge_and_deleted_remote_branch_do_not_block_cleanup(self):
        (self.path / 'tracked').write_text('task change')
        b.git(str(self.path), 'commit', '-am', 'task change')
        head = b.git(str(self.path), 'rev-parse', 'HEAD')
        b.git(str(self.path), 'push', 'origin', 'HEAD:refs/heads/task')
        self.git('merge', '--squash', self.task()['branch'])
        self.git('commit', '-qm', 'squash task')
        self.git('push', 'origin', 'main', ':refs/heads/task')
        self.assertNotEqual(self.git('rev-parse', 'main'), head)
        self.api.api.return_value = [dict(number=8, state='closed', merged_at='2026-01-01',
            head={'ref': self.task()['branch'], 'repo': {'id': 1}}, base={'repo': {'id': 1}})]
        self.assertEqual(self.retire()['state'], 'removed')
        self.assertEqual(self.git('rev-parse', self.task()['branch']), head)

    def test_open_alias_unknown_and_missing_github_responses_block(self):
        with self.state.db:
            self.state.db.execute('INSERT INTO items VALUES (8,?)', ('issue-7',))
        self.updates['8'] = {'state': 'OPEN'}
        self.blocked('alias is still open')
        del self.updates['8']
        self.blocked('unavailable')
        self.updates['8'] = {'state': 'DELETED'}
        self.blocked('unverified')

    def test_cleanup_retries_old_process_error_without_host_scans_or_remote_fetch(self):
        c.record(self.state, self.task(), 'blocked', 'Cannot inspect process 123: permission denied')
        real_git = b.git
        def local_only(cwd, *args):
            self.assertNotIn(args[0], ('fetch', 'ls-remote', 'for-each-ref'))
            return real_git(cwd, *args)
        # External operations go through Git and the explicit GitHub double only.
        # Container inventory is doubled; no /proc traversal or remote Git fetch is needed.
        run = c.subprocess.run
        def git_only(args, *a, **kw):
            self.assertEqual(args[0], 'git')
            return run(args, *a, **kw)
        with patch.object(b, 'git', side_effect=local_only), \
                patch.object(c.subprocess, 'run', side_effect=git_only), \
                patch.object(Path, 'iterdir', side_effect=PermissionError('protected /proc')):
            self.assertEqual(self.retire()['state'], 'removed')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.state.row(self.ident)['state'], 'succeeded')

    def test_github_errors_preserve_worktree(self):
        self.api.api.side_effect = RuntimeError('GitHub unavailable')
        self.blocked('GitHub unavailable')

    def test_open_task_is_retained_without_removal(self):
        self.updates['7']['state'] = 'OPEN'
        self.retire()
        self.assertTrue(self.path.exists())

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
        original = c.validate
        def changed(*args):
            result = original(*args)
            (self.path / 'late-work').write_text('preserve')
            return result
        with patch.object(c, 'validate', side_effect=changed):
            self.blocked('untracked')
        self.assertEqual((self.path / 'late-work').read_text(), 'preserve')

    def test_identical_block_is_persisted_once_and_symlink_replacement_is_rejected(self):
        (self.path / 'tracked').write_text('keep changes')
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
        with patch.object(b, 'checkout', side_effect=existing_only):
            result = self.retire()
            self.assertEqual(result['state'], 'removed')
            self.assertIn('already absent and unregistered', result['reason'])
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
            self.blocked('reappeared during cleanup checks')

    def test_missing_directory_permission_error_cannot_authorize_absence(self):
        self.git('worktree', 'remove', str(self.path))
        original = Path.lstat
        def denied(path, *args, **kwargs):
            if path == self.path:
                raise PermissionError('cannot stat worktree')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', denied):
            self.assertEqual(self.retire()['state'], 'blocked')

    def test_task_containers_are_removed_before_worktree_with_durable_resource_plan(self):
        plans = [dict(runtime=['fixture-docker'], containers=['task-app'], networks=[], volumes=[])]
        self.container_plan.side_effect = [plans, []]
        def remove(recorded):
            self.assertTrue(self.path.exists())
            saved = json.loads(self.task()['cleanup_json'])
            self.assertEqual(saved['state'], 'removing')
            self.assertEqual(saved['resources'], recorded)
        with patch.object(c, 'remove_containers', side_effect=remove) as cleanup:
            self.assertEqual(self.retire()['state'], 'removed')
            cleanup.assert_called_once_with(plans)
        self.assertFalse(self.path.exists())

    def test_container_failure_retains_worktree_plan_and_never_claims_success_or_replays(self):
        plans = [dict(runtime=['fixture-docker'], containers=['task-app'], networks=[], volumes=[])]
        self.container_plan.return_value = plans
        with patch.object(c, 'remove_containers', side_effect=ValueError('container removal failed')) as cleanup:
            result = self.retire()
            self.assertEqual(result['state'], 'removing')
            self.assertEqual(result['resources'], plans)
            self.assertIn('container removal failed', result['reason'])
            self.assertEqual(self.retire()['state'], 'removing')
            cleanup.assert_called_once()
        self.assertTrue(self.path.exists())
        self.assertEqual(self.state.row(self.ident)['state'], 'succeeded')

    def test_container_inventory_failure_does_not_remove_worktree(self):
        self.container_plan.side_effect = ValueError('runtime unavailable')
        self.blocked('runtime unavailable')

    def test_absent_worktree_still_cleans_orphaned_task_containers(self):
        self.git('worktree', 'remove', str(self.path))
        plans = [dict(runtime=['fixture-docker'], containers=['orphan'], networks=[], volumes=[])]
        self.container_plan.side_effect = [plans, []]
        with patch.object(c, 'remove_containers') as cleanup:
            self.assertEqual(self.retire()['state'], 'removed')
            cleanup.assert_called_once_with(plans)

    def test_remaining_containers_prevent_success_and_worktree_removal(self):
        self.container_plan.return_value = [dict(runtime=['fixture-docker'], containers=['task-app'], networks=[], volumes=[])]
        with patch.object(c, 'remove_containers'):
            self.assertEqual(self.retire()['state'], 'removing')
        self.assertTrue(self.path.exists())


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.cwd = '/tmp/fixture-task'
        self.containers, self.networks, self.volumes, self.removed = {}, {}, {}, []
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        cli = patch.object(c.shutil, 'which', side_effect=lambda name: name if name == 'docker' else None)
        cli.start()
        self.addCleanup(cli.stop)
        sockets = patch.object(Path, 'exists', return_value=False)
        sockets.start()
        self.addCleanup(sockets.stop)
        self.real_runtime_call = c.runtime_call
        calls = patch.object(c, 'runtime_call', side_effect=self.call)
        self.calls = calls.start()
        self.addCleanup(calls.stop)

    def container(self, ident, directory=None, project=None, mounts=None, labels=None):
        labels = dict(labels or {})
        if directory:
            labels[c.DIRECTORY_LABEL] = directory
        if project:
            labels[c.PROJECT_LABEL] = project
        self.containers[ident] = dict(Id=ident, Config={'Labels': labels}, Mounts=mounts or [],
                                      State={'Running': True}, NetworkSettings={'Networks': {}})
        return self.containers[ident]

    def resource(self, kind, ident, project=None, task=False):
        labels = {c.PROJECT_LABEL: project} if project else {}
        if task:
            labels[c.TASK_LABEL] = self.cwd
        resource = dict(Name=ident, Id=ident, Labels=labels, CreatedAt='2026-01-01T00:00:00Z', Containers={})
        (self.networks if kind == 'network' else self.volumes)[ident] = resource
        return resource

    def call(self, runtime, *args):
        if args[:2] == ('context', 'inspect'):
            return json.dumps('unix:///fixture.sock')
        self.assertTrue('--host' in runtime or '--remote=false' in runtime)
        if args[0] == 'ps':
            return '\n'.join(self.containers)
        kind, verb = args[:2]
        objects = {'container': self.containers, 'network': self.networks, 'volume': self.volumes}[kind]
        if verb == 'inspect':
            records = [objects[ident] for ident in args[2:]]
            if runtime[0] == 'podman' and kind == 'network':
                records = [{key.lower(): value for key, value in item.items()} for item in records]
            return json.dumps(records)
        if verb == 'ls':
            key, value = args[-1].removeprefix('label=').split('=', 1)
            return '\n'.join(ident for ident, item in objects.items() if item['Labels'].get(key) == value)
        self.assertEqual(verb, 'rm')
        ident = args[-1]
        if kind == 'container':
            self.assertIn('--force', args)
            self.assertIn('--volumes', args)
        else:
            self.assertNotIn('--force', args)
        self.removed.append((kind, ident))
        del objects[ident]
        return ident

    def test_running_compose_services_and_private_resources_removed_without_other_project(self):
        self.container('task-app', self.cwd, 'task')
        self.container('task-db', self.cwd, 'task')['State']['Running'] = False
        self.container('other-app', '/tmp/other-task', 'other')
        self.resource('network', 'task-net', 'task')
        self.resource('volume', 'task-data', 'task')
        self.resource('network', 'other-net', 'other')
        self.resource('volume', 'other-data', 'other')
        plans = c.container_plan(self.cwd)
        self.assertEqual(plans[0]['containers'], ['task-app', 'task-db'])
        c.remove_containers(plans)
        self.assertEqual(self.removed, [('container', 'task-app'), ('container', 'task-db'),
                                       ('network', 'task-net'), ('volume', 'task-data')])
        self.assertEqual(set(self.containers), {'other-app'})
        self.assertEqual(set(self.networks), {'other-net'})
        self.assertEqual(set(self.volumes), {'other-data'})
        self.assertEqual(c.container_plan(self.cwd), [])

    def test_standalone_bind_mount_and_explicit_task_labels_are_owned(self):
        self.container('mounted', mounts=[dict(Type='bind', Source=self.cwd + '/src')])
        self.container('tagged', labels={c.TASK_LABEL: self.cwd})
        self.container('shared-parent', mounts=[dict(Type='bind', Source='/tmp')])
        self.container('sibling-prefix', mounts=[dict(Type='bind', Source=self.cwd + '-other')])
        c.remove_containers(c.container_plan(self.cwd))
        self.assertEqual(set(self.containers), {'shared-parent', 'sibling-prefix'})

    def test_same_compose_name_in_other_worktree_blocks_before_mutation(self):
        self.container('task', self.cwd, 'collision')
        self.container('other', '/tmp/other', 'collision')
        with self.assertRaisesRegex(ValueError, 'shared with another worktree'):
            c.container_plan(self.cwd)
        self.assertEqual(self.removed, [])

    def test_conflicting_explicit_ownership_does_not_kill_another_project(self):
        self.container('foreign', '/tmp/other', 'other',
                       mounts=[dict(Type='bind', Source=self.cwd)])
        with self.assertRaisesRegex(ValueError, 'conflicting task ownership'):
            c.container_plan(self.cwd)
        self.assertEqual(self.removed, [])

    def test_shared_volumes_and_networks_survive_including_stopped_foreign_containers(self):
        self.container('task', self.cwd, 'task')
        other = self.container('other', '/tmp/other', 'other',
                               mounts=[dict(Type='volume', Name='shared-data')])
        other['State']['Running'] = False
        other['NetworkSettings']['Networks'] = {'shared-net': {}}
        self.resource('volume', 'shared-data', 'task')
        self.resource('network', 'shared-net', 'task')
        c.remove_containers(c.container_plan(self.cwd))
        self.assertEqual(self.removed, [('container', 'task')])
        self.assertIn('shared-data', self.volumes)
        self.assertIn('shared-net', self.networks)

    def test_tagged_orphan_resources_are_removed_without_existing_container_or_directory(self):
        self.resource('network', 'orphan-net', task=True)
        self.resource('volume', 'orphan-data', task=True)
        c.remove_containers(c.container_plan(self.cwd))
        self.assertEqual(self.removed, [('network', 'orphan-net'), ('volume', 'orphan-data')])

    def test_volume_recreated_after_inventory_is_not_removed(self):
        self.resource('volume', 'data', task=True)
        plans = c.container_plan(self.cwd)
        self.volumes['data']['CreatedAt'] = '2026-02-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'volume identity changed'):
            c.remove_containers(plans)
        self.assertEqual(self.removed, [])

    def test_remote_docker_is_never_mutated_and_podman_is_forced_local(self):
        with patch.dict(os.environ, DOCKER_HOST='tcp://example.invalid:2375'):
            with self.assertRaisesRegex(ValueError, 'local Docker endpoint'):
                c.container_plan(self.cwd)
        with patch.object(c.shutil, 'which', side_effect=lambda name: name if name == 'podman' else None):
            c.container_plan(self.cwd)
        self.assertTrue(any(call.args[0] == ['podman', '--remote=false'] for call in self.calls.call_args_list))
        self.assertEqual(self.removed, [])

    def test_docker_context_takes_precedence_and_endpoint_is_pinned(self):
        self.container('task', self.cwd, 'task')
        with patch.dict(os.environ, DOCKER_CONTEXT='fixture-local', DOCKER_HOST='tcp://unused.invalid'):
            plans = c.container_plan(self.cwd)
        self.assertEqual(plans[0]['runtime'], ['docker', '--host', 'unix:///fixture.sock'])
        self.assertIn(unittest.mock.call(['docker'], 'context', 'inspect', 'fixture-local',
            '--format', '{{json .Endpoints.docker.Host}}'), self.calls.call_args_list)

    def test_podman_network_json_and_private_volume_cleanup(self):
        self.container('task', self.cwd, 'task')
        self.resource('network', 'task-net', 'task')
        self.resource('volume', 'task-data', 'task')
        with patch.object(c.shutil, 'which', side_effect=lambda name: name if name == 'podman' else None):
            c.remove_containers(c.container_plan(self.cwd))
        self.assertEqual(self.removed, [('container', 'task'), ('network', 'task-net'), ('volume', 'task-data')])

    def test_missing_cli_with_runtime_socket_is_not_reported_as_clean(self):
        with patch.object(c.shutil, 'which', return_value=None), patch.object(Path, 'exists', return_value=True):
            with self.assertRaisesRegex(ValueError, 'cleanup CLI is unavailable'):
                c.container_plan(self.cwd)

    def test_runtime_failure_is_reported_before_any_deletion(self):
        self.calls.side_effect = ValueError('runtime unavailable')
        with self.assertRaisesRegex(ValueError, 'runtime unavailable'):
            c.container_plan(self.cwd)
        self.assertEqual(self.removed, [])

    def test_runtime_subprocess_timeout_and_pinned_endpoint_environment(self):
        with patch.dict(os.environ, DOCKER_CONTEXT='unrelated'), \
                patch.object(c.subprocess, 'run', return_value=Mock(returncode=1, stderr='runtime unavailable')) as run:
            with self.assertRaisesRegex(ValueError, 'runtime unavailable'):
                self.real_runtime_call(['docker', '--host', 'unix:///fixture.sock'], 'container', 'rm', 'task')
            self.assertEqual(run.call_args.kwargs['timeout'], 25)
            self.assertNotIn('DOCKER_CONTEXT', run.call_args.kwargs['env'])


if __name__ == '__main__':
    unittest.main()
