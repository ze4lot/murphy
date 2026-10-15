#!/usr/bin/env python3
"""Test-only gh double: fictional users, local files and no network."""
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
directory = Path(os.environ.get('GH_CONFIG_DIR', str(Path.home() / '.config/gh')))
token_present = any(os.environ.get(key) for key in ('GH_TOKEN', 'GITHUB_TOKEN'))
if os.environ.get('MURPHY_TEST_GH_LOG'):
    with open(os.environ['MURPHY_TEST_GH_LOG'], 'a') as log:
        log.write(json.dumps({'args': args, 'directory': str(directory), 'token_present': token_present}) + '\n')
if args[:2] == ['auth', 'login']:
    directory.mkdir(parents=True, exist_ok=True)
    user = json.loads(os.environ.get('MURPHY_TEST_LOGIN_USER',
        '{"id":2001,"login":"example-executor","name":"Example Executor","type":"User"}'))
    (directory / 'user.json').write_text(json.dumps(user))
    raise SystemExit(0)
if token_present:
    # Simulate inherited human tokens taking precedence over the executor login.
    directory = Path.home() / '.config/gh'
try:
    user = json.loads((directory / 'user.json').read_text())
except FileNotFoundError:
    print('Fake gh: authentication missing', file=sys.stderr)
    raise SystemExit(1)
if args and args[0] == 'api' and args[-1] == 'user':
    print(json.dumps(user))
elif args == ['auth', 'git-credential', 'get']:
    sys.stdin.read()
    print('username=' + user['login'] + '\npassword=fictional-executor-credential\n')
else:
    print('Fake gh: unsupported command', file=sys.stderr)
    raise SystemExit(1)
