#!/usr/bin/env python3
"""Test-only CLI double. Its executions never call a model."""
import json
import os
import subprocess
import sys
import time
import uuid

prompt = sys.stdin.read()
thread = sys.argv[sys.argv.index('resume') + 1] if 'resume' in sys.argv else str(uuid.uuid4())

def emit(kind, **data):
    print(json.dumps({'type': kind, **data}), flush=True)

emit('thread.started', thread_id=thread, argv=sys.argv, prompt=prompt)
emit('turn.started', turn_id=str(uuid.uuid4()))
if 'ASSERT_EXECUTOR_ENV' in prompt:
    user = json.loads(subprocess.check_output(['gh', 'api', '--hostname', 'github.com', 'user'], text=True))
    identity = subprocess.check_output(['git', 'var', 'GIT_AUTHOR_IDENT'], text=True)
    credentials = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
                                 capture_output=True, text=True, check=True).stdout
    if (user['id'] != 2001 or '2001+example-executor@users.noreply.github.com' not in identity
            or 'username=example-executor' not in credentials or any(os.environ.get(key) for key in
            ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'))):
        emit('error', message='Executor did not receive an isolated Git/GitHub identity')
        sys.exit(1)
if 'NO_COMPLETION' in prompt:
    sys.exit(0)
if 'WRONG_THREAD' in prompt:
    emit('thread.started', thread_id=str(uuid.uuid4()))
if 'SLOW' in prompt:
    time.sleep(1)
if 'DESCENDANT' in prompt:
    # Detach and close inherited output: the worker must still wait for this descendant.
    code = 'import time; time.sleep(1)'
    subprocess.Popen([sys.executable, '-c', code], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=False)
if 'DAEMON_FOREVER' in prompt:
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=False)
if any(marker in prompt for marker in ('WRITE_TASK_FILE', 'COMMIT_TASK_FILE', 'COMMIT_EXECUTOR_FILE')):
    if 'default_permissions="murphy"' not in sys.argv or any(arg in sys.argv for arg in ('read-only', 'danger-full-access')):
        emit('error', message='Writable task permissions were not passed')
        sys.exit(1)
    with open('task.txt', 'a') as handle:
        handle.write('task change\n')
if 'COMMIT_TASK_FILE' in prompt or 'COMMIT_EXECUTOR_FILE' in prompt:
    subprocess.run(['git', 'add', 'task.txt'], check=True)
    identity = [] if 'COMMIT_EXECUTOR_FILE' in prompt else ['-c', 'user.name=Test Bot', '-c', 'user.email=bot@example.invalid']
    subprocess.run(['git', *identity, 'commit', '-qm', 'task implementation'], check=True, stdout=subprocess.DEVNULL)
if 'PUSH_TASK_BRANCH' in prompt:
    subprocess.run(['git', 'push', '-q', os.environ['MURPHY_TEST_REMOTE'], 'HEAD:refs/heads/codex/pilot'], check=True)
if 'SWITCH_TASK_BRANCH' in prompt:

    subprocess.run(['git', 'switch', '-qc', 'codex/unexpected'], check=True)

emit('item.completed', item={'type': 'agent_message', 'text': 'Pilot output; thread ' + thread})
emit('turn.completed')
