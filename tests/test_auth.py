#!/usr/bin/env python3
"""Real child/Git processes with fictional gh accounts and no external writes."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from murphy import auth, bridge as b
from murphy.manager import Manager, project_config


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        human = self.root / '.config/gh'
        human.mkdir(parents=True)
        self.human = human / 'user.json'
        self.human.write_text(json.dumps({'id': 3001, 'login': 'example-owner', 'type': 'User'}))
        self.bin = self.root / 'tools with spaces'
        self.bin.mkdir()
        self.gh = self.bin / 'gh'
        shutil.copyfile(ROOT / 'tests/fake_gh.py', self.gh)
        self.gh.chmod(0o700)
        self.log = self.root / 'gh-calls.jsonl'
        self.fallback_marker = self.root / 'human-helper-called'
        self.human_config = self.root / '.gitconfig'
        self.human_config.write_text('[user]\n name = Human\n email = human@example.invalid\n')
        self.fallback = self.root / 'human-credential-helper'
        self.fallback.write_text('#!/bin/sh\ntouch ' + shlex.quote(str(self.fallback_marker)) + '\n')
        self.fallback.chmod(0o700)
        subprocess.run(['git', 'config', '--file', str(self.human_config), '--add', 'credential.helper',
                        str(self.fallback)], check=True)
        self.original_config = self.human_config.read_bytes()
        self.env_patch = patch.dict(os.environ, {
            'HOME': str(self.root), 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
            'GH_TOKEN': 'fictional-inherited-human-token', 'GITHUB_TOKEN': 'fictional-other-human-token',
            'GIT_AUTHOR_NAME': 'Human', 'GIT_AUTHOR_EMAIL': 'human@example.invalid',
            'GIT_COMMITTER_NAME': 'Human', 'GIT_COMMITTER_EMAIL': 'human@example.invalid',
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'credential.helper',
            'GIT_CONFIG_VALUE_0': str(self.fallback), 'MURPHY_TEST_GH_LOG': str(self.log),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        # Do not inherit a developer's alternate gh config or login fixture.
        for key in ('GH_CONFIG_DIR', 'MURPHY_TEST_LOGIN_USER'):
            os.environ.pop(key, None)
        self.manager = Manager(self.root / 'state with spaces')
        self.addCleanup(self.manager.close)
        self.manager.init(gh=str(self.gh), codex=str(ROOT / 'tests/fake_codex.py'), separate_account=True)
        settings = self.manager.account.settings
        self.config = b.Config('example/project', 1001, settings['user_id'], settings['owner_id'],
                               settings['owner_login'], gh=str(self.gh), codex=settings['codex'],
                               gh_config_dir=settings['gh_config_dir'], git_name=settings['git_name'],
                               git_email=settings['git_email'])
        self.cwd = self.root / 'source'
        subprocess.run(['git', 'init', '-qb', 'codex/task', str(self.cwd)], check=True)
        subprocess.run(['git', '-C', str(self.cwd), 'remote', 'add', 'origin',
                        'git@github.com:example/project.git'], check=True)
        subprocess.run(['git', '-C', str(self.cwd), 'config', 'user.name', 'Local Human'], check=True)
        subprocess.run(['git', '-C', str(self.cwd), 'config', 'user.email', 'local-human@example.invalid'], check=True)
        subprocess.run(['git', '-C', str(self.cwd), 'commit', '--allow-empty', '-qm', 'fixture'], check=True)

    def credential(self, host='github.com'):
        return subprocess.run(['git', 'credential', 'fill'], cwd=self.cwd, env=auth.environment(self.config),
                              input=f'protocol=https\nhost={host}\n\n', capture_output=True, text=True, timeout=10)

    def test_two_accounts_and_owner_logout_leave_executor_usable(self):
        status = self.manager.auth_status()
        self.assertEqual(status['owner']['login'], 'example-owner')
        self.assertEqual(status['executor']['login'], 'example-executor')
        self.human.unlink()
        self.assertTrue(self.manager.auth_status()['authenticated'])
        with project_config(self.config):
            self.assertEqual(b.GitHub().api('user')['id'], 2001)
        self.assertEqual(os.environ['GH_TOKEN'], 'fictional-inherited-human-token')
        self.assertNotIn('GH_CONFIG_DIR', os.environ)

    def test_git_uses_executor_helper_and_identity_without_global_or_checkout_changes(self):
        result = self.credential()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('username=example-executor', result.stdout)
        self.assertFalse(self.fallback_marker.exists())
        hook_marker = self.root / 'repository-hook-called'
        hook = self.cwd / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\ntouch ' + shlex.quote(str(hook_marker)) + '\n')
        hook.chmod(0o700)
        with project_config(self.config):
            self.assertEqual(b.checkout(str(self.cwd))['branch'], 'codex/task')
            b.git(str(self.cwd), 'commit', '--allow-empty', '-qm', 'executor commit')
            self.assertEqual(b.git(str(self.cwd), 'log', '-1', '--format=%an <%ae>|%cn <%ce>'),
                             'Example Executor <2001+example-executor@users.noreply.github.com>|'
                             'Example Executor <2001+example-executor@users.noreply.github.com>')
            bare = self.root / 'remote.git'
            subprocess.run(['git', 'init', '--bare', '-q', str(bare)], check=True)
            b.git(str(self.cwd), 'push', str(bare), 'HEAD:refs/heads/codex/task')
        self.assertTrue(hook_marker.exists())
        pushed_author = subprocess.check_output(['git', '--git-dir', str(bare), 'log', 'codex/task', '-1',
                                                  '--format=%ae'], text=True).strip()
        self.assertEqual(pushed_author, '2001+example-executor@users.noreply.github.com')
        self.assertEqual(self.human_config.read_bytes(), self.original_config)
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.cwd), 'config', '--local',
                                                  'user.name'], text=True).strip(), 'Local Human')
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.cwd), 'config', '--local',
                                                  'remote.origin.url'], text=True).strip(),
                         'git@github.com:example/project.git')
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        credential_calls = [c for c in calls if c['args'] == ['auth', 'git-credential', 'get']]
        self.assertTrue(credential_calls)
        self.assertTrue(all(not call['token_present'] for call in credential_calls))
        self.assertTrue(all(call['directory'] == self.config.gh_config_dir for call in credential_calls))

    def test_repository_authorization_headers_block_source_and_linked_checkouts(self):
        linked = self.root / 'linked'
        subprocess.run(['git', '-C', str(self.cwd), 'worktree', 'add', '-qb', 'codex/linked', str(linked)],
                       check=True)
        subprocess.run(['git', '-C', str(self.cwd), 'config',
                        'http.https://github.com/example/project.git.extraHeader',
                        'aUtHoRiZaTiOn: Basic fictional-owner-token'], check=True)
        original = (self.cwd / '.git/config').read_bytes()
        with project_config(self.config):
            for cwd in (self.cwd, linked):
                with self.subTest(cwd=cwd), self.assertRaisesRegex(ValueError, 'repository Authorization headers') as error:
                    b.checkout(str(cwd))
                self.assertNotIn('fictional-owner-token', str(error.exception))
        self.assertEqual((self.cwd / '.git/config').read_bytes(), original)
        self.assertFalse(self.fallback_marker.exists())

    def test_replayed_commit_keeps_original_author_and_executor_committer(self):
        subprocess.run(['git', '-C', str(self.cwd), 'switch', '-qc', 'original-author'], check=True)
        (self.cwd / 'original.txt').write_text('original author work\n')
        subprocess.run(['git', '-C', str(self.cwd), 'add', 'original.txt'], check=True)
        subprocess.run(['git', '-C', str(self.cwd), 'commit', '-qm', 'original'], check=True)
        original = subprocess.check_output(['git', '-C', str(self.cwd), 'rev-parse', 'HEAD'], text=True).strip()
        subprocess.run(['git', '-C', str(self.cwd), 'switch', '-q', 'codex/task'], check=True)
        with project_config(self.config):
            b.git(str(self.cwd), 'cherry-pick', original)
            self.assertEqual(b.git(str(self.cwd), 'log', '-1', '--format=%an <%ae>|%cn <%ce>'),
                             'Human <human@example.invalid>|'
                             'Example Executor <2001+example-executor@users.noreply.github.com>')

    def test_changed_or_missing_executor_cannot_fall_back_to_human_credentials(self):
        saved = Path(self.config.gh_config_dir) / 'user.json'
        saved.write_text(json.dumps({'id': 2002, 'login': 'wrong-executor', 'type': 'User'}))
        result = self.credential()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('password=', result.stdout)
        self.assertFalse(self.fallback_marker.exists())
        saved.unlink()
        status = self.manager.auth_status()
        self.assertFalse(status['authenticated'])
        self.assertIn('murphy auth login', status['error'])
        result = self.credential()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('password=', result.stdout)
        self.assertFalse(self.fallback_marker.exists())

    def test_executor_credentials_are_not_supplied_for_another_host(self):
        result = self.credential('example.invalid')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('password=', result.stdout)
        self.assertFalse(self.fallback_marker.exists())

    def test_codex_child_inherits_executor_context_for_plain_git_and_gh(self):
        with project_config(self.config):
            state = b.State(self.root / 'execution')
            self.addCleanup(state.db.close)
            state.bind_config(self.config)
            class Api:
                def identity(self):
                    pass
                def latest(self):
                    return None
            state.initialize(Api())
            state.register('task', self.cwd, 'codex/task')
            command = state.enqueue('task', 'start', 'ASSERT_EXECUTOR_ENV')
            b.Worker(state, codex=self.config.codex, timeout=5).run(once=True)
        self.assertEqual(state.row(command)['state'], 'succeeded', state.row(command)['detail'])
        self.assertFalse(self.fallback_marker.exists())
        self.assertEqual(self.human_config.read_bytes(), self.original_config)


if __name__ == '__main__':
    unittest.main(verbosity=2)
