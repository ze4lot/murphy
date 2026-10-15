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
if 'CHANGE_CHECKOUT' in prompt:
    open('unexpected.txt', 'w').write('preserve me')
emit('item.completed', item={'type': 'agent_message', 'text': 'Pilot output; thread ' + thread})
emit('turn.completed')
