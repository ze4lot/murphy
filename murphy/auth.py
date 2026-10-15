"""Process-scoped executor identity and a Git credential helper.

Executed directly by Git as well as imported; keep this module stdlib-only.
Credentials remain owned by gh and only travel through Git's helper pipe.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


def environment(settings):
    values = settings if isinstance(settings, dict) else vars(settings)
    env = dict(os.environ)
    directory = values.get('gh_config_dir')
    if not directory:
        return env
    for key in list(env):
        if (key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN',
                    'GH_CONFIG_DIR', 'GH_HOST', 'GH_REPO', 'GH_DEBUG', 'GH_PROMPT_DISABLED',
                    'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL',
                    'GIT_CONFIG', 'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_ASKPASS', 'SSH_ASKPASS', 'SSH_AUTH_SOCK',
                    'EMAIL') or key.startswith(('GIT_CONFIG_', 'GIT_TRACE'))):
            env.pop(key, None)
    env.update(GH_CONFIG_DIR=directory, GH_HOST='github.com', GH_PROMPT_DISABLED='1',
               GH_NO_UPDATE_NOTIFIER='1', GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    if values.get('git_name') and values.get('git_email'):
        gh = values['gh']
        ident = values.get('bot_user_id', values.get('user_id'))
        helper = '!' + shlex.join([sys.executable, str(Path(__file__).resolve()), gh, str(ident)])
        options = [('credential.helper', ''), ('credential.helper', helper),
                   ('http.extraHeader', ''), ('http.https://github.com/.extraHeader', ''),
                   ('user.name', values['git_name']), ('user.email', values['git_email']),
                   ('url.https://github.com/.insteadOf', 'git@github.com:'),
                   ('url.https://github.com/.insteadOf', 'ssh://git@github.com/')]
        env['GIT_CONFIG_COUNT'] = str(len(options))
        for index, (key, value) in enumerate(options):
            env[f'GIT_CONFIG_KEY_{index}'] = key
            env[f'GIT_CONFIG_VALUE_{index}'] = value
        # Git defaults new commits to the executor's config, but remains free to
        # restore the original author when cherry-picking/rebasing existing work.
    return env


def login(settings):
    """Authenticate in the executor's directory, never the user's default gh."""
    env = environment(settings)
    env.pop('GH_PROMPT_DISABLED', None)
    subprocess.run([settings['gh'], 'auth', 'login', '--hostname', 'github.com',
                    '--web', '--git-protocol', 'https', '--skip-ssh-key'], env=env, check=True, timeout=600)


def credential_helper():
    if len(sys.argv) != 4 or sys.argv[3] != 'get':
        return 0
    request = sys.stdin.read()
    fields = dict(line.split('=', 1) for line in request.splitlines() if '=' in line)
    if fields.get('protocol') != 'https' or fields.get('host') != 'github.com':
        return 1
    gh, expected = sys.argv[1:3]
    try:
        user = subprocess.run([gh, 'api', '--hostname', 'github.com', 'user'],
                              capture_output=True, text=True, timeout=25)
        if user.returncode or str(json.loads(user.stdout)['id']) != expected:
            raise ValueError('Unexpected executor identity')
        result = subprocess.run([gh, 'auth', 'git-credential', 'get'], input=request,
                                capture_output=True, text=True, timeout=25)
        if result.returncode:
            raise ValueError('Executor credential unavailable')
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        print('Murphy executor authentication failed; run murphy auth status.', file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(credential_helper())
