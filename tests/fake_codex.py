#!/usr/bin/env python3
"""Test-only CLI double. Its executions never call a model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

if sys.argv[1:] == ['app-server']:
    # Account metadata only: this branch never creates a model execution/session.
    for line in sys.stdin:
        request = json.loads(line)
        if request['method'] == 'initialize':
            assert request['params']['clientInfo']['name'] == 'murphy_monitor'
            print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
        elif request['method'] == 'account/rateLimits/read':
            mode = os.environ.get('MURPHY_TEST_LIMITS_MODE', '')
            if mode == 'timeout':
                time.sleep(30)
            elif mode == 'invalid':
                print('invalid JSON', flush=True)
            elif mode == 'error':
                print(json.dumps({'id': request['id'], 'error': {'message': 'Not signed in'}}), flush=True)
            else:
                limits = {'primary': {'usedPercent': 25, 'windowDurationMins': 300},
                          'secondary': {'usedPercent': 81, 'windowDurationMins': 10080}}
                result = {'rateLimits': {'primary': {'usedPercent': 99, 'windowDurationMins': 60}}}
                if mode != 'legacy':
                    result['rateLimitsByLimitId'] = {'codex': limits}
                print(json.dumps({'method': 'account/rateLimits/updated', 'params': {}}), flush=True)
                print(json.dumps({'id': request['id'], 'result': result}), flush=True)
        else:
            assert request['method'] == 'initialized'
    sys.exit(0)

prompt = sys.stdin.read()
thread = sys.argv[sys.argv.index('resume') + 1] if 'resume' in sys.argv else str(uuid.uuid4())

def emit(kind, **data):
    print(json.dumps({'type': kind, **data}), flush=True)

emit('thread.started', thread_id=thread, argv=sys.argv, prompt=prompt, cwd=os.getcwd())
emit('turn.started', turn_id=str(uuid.uuid4()))
if 'CHECK_WORKSPACE_MODES' in prompt:
    Path('runtime-directory').mkdir(exist_ok=True)
    Path('runtime-directory/file.txt').write_text('readable by application containers')
if 'LEAVE_SSH_SOCKET' in prompt:
    import socket
    import tomllib
    value = next(arg for arg in sys.argv if arg.startswith('shell_environment_policy.set.MURPHY_SSH_CONTROL_DIR='))
    directory = tomllib.loads(value)['shell_environment_policy']['set']['MURPHY_SSH_CONTROL_DIR']
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(Path(directory) / 'test-socket'))
schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
assert set(schema['properties']['outcome']['enum']) == {'succeeded', 'waiting_for_user'}
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
if 'WAIT_FOR_TEST_RELEASE' in prompt:
    Path('execution-started').write_text(str(os.getpid()))
    while not Path('execution-release').exists():
        time.sleep(0.05)
    Path('execution-started').unlink()
    Path('execution-release').unlink()
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

result = {'outcome': 'succeeded', 'message': 'Pilot output; thread ' + thread}
if 'ASK_OWNER' in prompt:
    result = {'outcome': 'waiting_for_user', 'message': 'Specify which option to use: A or B.'}
if 'SUCCESS_WITH_QUESTION_MARK' in prompt:
    result['message'] = 'Verified handling of the question mark: ?'
text = json.dumps(result)
if 'INVALID_TURN_RESULT' in prompt:
    text = 'Which option should I use?'
emit('item.completed', item={'type': 'agent_message', 'text': text})
emit('turn.completed')
