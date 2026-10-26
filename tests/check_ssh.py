#!/usr/bin/env python3
"""Local OpenSSH + native Codex sandbox check; no models or external services."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from murphy import bridge as b


def run(args, **kwargs):
    kwargs.setdefault('check', True)
    return subprocess.run([str(arg) for arg in args], timeout=30, **kwargs)


def probe():
    fixture = json.loads(Path(os.environ['MURPHY_TEST_FIXTURE']).read_text())
    config = fixture['config']
    directory = Path(os.environ['MURPHY_SSH_CONTROL_DIR'])
    # The restart turn may have one dead socket, as after an unclean host stop.
    existing = [path for path in directory.iterdir() if path.is_socket()]
    assert len(existing) == (0 if fixture['wait_for_idle'] else 1), existing
    ssh = ['ssh', '-F', config]
    run([*ssh, 'local-test', 'true'])  # ControlMaster auto creates the master itself.
    sockets = [path for path in directory.iterdir() if path.is_socket()]
    assert len(sockets) == 1 and sockets[0].is_socket(), sockets
    control = sockets[0]
    inode = control.stat().st_ino
    # Prove reuse: subsequent clients have NO usable authentication key.
    reuse = ['-o', 'PubkeyAuthentication=no', '-o', 'IdentityAgent=none']
    run([*ssh, *reuse, 'local-test', 'true'])
    Path('payload.txt').write_text('sandbox scp payload')
    run(['scp', '-F', config, *reuse, 'payload.txt', 'local-test:' + fixture['upload']])
    run(['scp', '-F', config, *reuse, 'local-test:' + fixture['upload'], 'download.txt'])
    assert Path('download.txt').read_text() == 'sandbox scp payload'
    assert control.stat().st_ino == inode
    for path in fixture['protected']:
        try:
            Path(path).write_text('must not change')
        except PermissionError:
            pass
        except OSError as error:
            assert error.errno == 30, error  # Read-only filesystem.
        else:
            raise AssertionError('Unexpected write access: ' + path)
    # %C must distinguish remote users and ports, without contacting either target.
    paths = []
    for extra in ([], ['-l', 'another-user'], ['-p', '1'], ['-o', 'HostName=127.0.0.2']):
        result = run([*ssh, *extra, '-G', 'local-test'], capture_output=True, text=True)
        paths.append(next(line for line in result.stdout.splitlines() if line.startswith('controlpath ')))
    assert len(set(paths)) == 4, paths
    # Keep host-key checking effective even with multiplexing configured.
    bad = run([*ssh, '-o', 'ControlPath=none', '-o', 'UserKnownHostsFile=' + fixture['bad_hosts'],
               'local-test', 'true'], capture_output=True, text=True, check=False)
    assert bad.returncode != 0 and 'Host key verification failed' in bad.stderr, bad.stderr
    if fixture['wait_for_idle']:
        deadline = time.monotonic() + 10
        while control.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not control.exists(), 'OpenSSH did not remove its idle master/socket'
        print('PASS: native ControlPersist idle expiry removes the socket')
    # The other turn returns immediately: sandbox teardown can leave a dead
    # socket, which the real worker must remove after draining descendants.
    print('PASS: cold SSH start, authenticated reuse, SCP both ways, isolation, host-key checking')


def codex_double():
    """Replace ONLY model output; execute commands with the actual worker profile."""
    sys.stdin.read()
    config_args = []
    env = dict(os.environ)
    for index, arg in enumerate(sys.argv):
        if arg == '--config':
            value = sys.argv[index + 1]
            config_args += [arg, value]
            settings = tomllib.loads(value)
            env.update(settings.get('shell_environment_policy', {}).get('set', {}))
    thread = sys.argv[sys.argv.index('resume') + 1] if 'resume' in sys.argv else str(uuid.uuid4())
    run([env['MURPHY_TEST_CODEX'], 'sandbox', '--permission-profile', 'murphy', *config_args,
         '--cd', os.getcwd(), '--', sys.executable, __file__, '--probe'],
        env=env, stdout=sys.stderr)
    print(json.dumps({'type': 'thread.started', 'thread_id': thread}), flush=True)
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
          'text': json.dumps({'outcome': 'succeeded', 'message': 'Local SSH sandbox probe passed'})}}), flush=True)
    print(json.dumps({'type': 'turn.completed'}), flush=True)


def worker_turn(root, verb):
    b.CONFIG = b.Config('example/project', 1001, 2001, 3001, 'example-owner')
    state = b.State(root / 'state')
    try:
        if state.setting('baseline') is None:
            state.bind_config(b.CONFIG)
            with state.db:
                state.set('baseline', 0)
            state.register('probe', root / 'task', 'codex/check')
        ident = state.enqueue('probe', verb, 'Run the local SSH probe')
        b.Worker(state, codex=str(root / 'codex-double'), timeout=30).run(once=True)
        row = state.row(ident)
        print((root / 'state' / (row['execution_id'] + '.stderr')).read_text(), end='', file=sys.stderr)
        assert row['state'] == 'succeeded', dict(row)
        assert not b.descendants(), b.descendants()
    finally:
        state.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--sshd', default=shutil.which('sshd') or '/usr/sbin/sshd')
    args = parser.parse_args()
    for tool in (args.codex, args.sshd, 'ssh', 'scp', 'ssh-keygen'):
        if not shutil.which(tool):
            parser.error('Missing test prerequisite: ' + tool)
    # Outside /tmp: the sandbox must keep fixture credentials and sibling files read-only.
    with tempfile.TemporaryDirectory(prefix='murphy-ssh-check-', dir=Path.home()) as temporary:
        root = Path(temporary)
        for name in ('host', 'client', 'wrong-host'):
            run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', root / name])
        (root / 'authorized_keys').write_text((root / 'client.pub').read_text())
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        (root / 'known_hosts').write_text(f'[127.0.0.1]:{port} ' + (root / 'host.pub').read_text())
        (root / 'bad_hosts').write_text(f'[127.0.0.1]:{port} ' + (root / 'wrong-host.pub').read_text())
        config = root / 'sshd_config'
        config.write_text(f"""ListenAddress 127.0.0.1
Port {port}
HostKey {root / 'host'}
AuthorizedKeysFile {root / 'authorized_keys'}
PidFile none
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes yes
LogLevel VERBOSE
Subsystem sftp internal-sftp
""")
        client = root / 'ssh_config'
        client.write_text(f'''Match originalhost local-test exec "test x${{MURPHY_SSH_CONTROL_DIR:+set}} = xset"
    ControlMaster auto
    ControlPersist 2
    ControlPath ${{MURPHY_SSH_CONTROL_DIR}}/%C
Match all

Host local-test
    HostName 127.0.0.1
    Port {port}
    User {pwd.getpwuid(os.getuid()).pw_name}
    IdentityFile {root / 'client'}
    IdentityAgent none
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking yes
    UserKnownHostsFile {root / 'known_hosts'}
    GlobalKnownHostsFile /dev/null
''')
        outside = dict(os.environ)
        outside.pop('MURPHY_SSH_CONTROL_DIR', None)
        ordinary = run(['ssh', '-F', client, '-G', 'local-test'], env=outside, capture_output=True, text=True)
        assert 'controlmaster false' in ordinary.stdout, ordinary.stdout
        source, task = root / 'unrelated-project', root / 'project'
        source.mkdir()
        (source / 'human.txt').write_text('preserve')
        run(['git', 'init', '-q', '-b', 'codex/check', task])
        run(['git', '-C', task, '-c', 'user.name=Test Bot', '-c', 'user.email=bot@example.invalid',
             'commit', '--allow-empty', '-qm', 'fixture'])
        run(['git', '-C', task, 'remote', 'add', 'origin', 'https://github.com/example/project.git'])
        b.CONFIG = b.Config('example/project', 1001, 2001, 3001, 'example-owner')
        before = b.checkout(str(task))
        directory = b.ssh_control_directory(before)
        other = b.ssh_control_directory(dict(before, common_dir=str(root / 'other.git')))
        (other / 'sibling').write_text('preserve')
        fixture = root / 'fixture.json'
        fixture_data = {'config': str(client), 'upload': str(root / 'uploaded'),
                        'bad_hosts': str(root / 'bad_hosts'),
                        'protected': [str(source / 'human.txt'), str(root / 'client'),
                                      str(client), str(other / 'sibling')]}
        driver = root / 'codex-double'
        driver.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path('
                          + repr(str(Path(__file__).resolve())) + ', run_name="__main__")\n')
        driver.chmod(0o700)
        env = dict(os.environ, MURPHY_TEST_CODEX=shutil.which(args.codex), MURPHY_TEST_FIXTURE=str(fixture))
        with (root / 'sshd.log').open('w+') as log:
            server = subprocess.Popen([str(Path(args.sshd).resolve()), '-D', '-e', '-f', str(config)],
                                      stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 5
                while True:
                    if server.poll() is not None:
                        log.seek(0)
                        raise AssertionError(log.read())
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                            break
                    except OSError:
                        assert time.monotonic() < deadline, 'Local sshd did not start'
                        time.sleep(0.05)
                for verb in ('start', 'continue'):
                    assert not list(directory.iterdir()), 'Previous master/socket was not cleaned up'
                    if verb == 'continue':
                        # Recreate the cache, then leave a dead listener at the exact
                        # %C path to model the filesystem surviving an abrupt restart.
                        directory.rmdir()
                        directory = b.ssh_control_directory(before)
                        options = run(['ssh', '-F', client, '-G', 'local-test'], capture_output=True,
                                      text=True, env=dict(env, MURPHY_SSH_CONTROL_DIR=str(directory)))
                        control = next(line.split(' ', 1)[1] for line in options.stdout.splitlines()
                                       if line.startswith('controlpath '))
                        with socket.socket(socket.AF_UNIX) as stale:
                            stale.bind(control)
                    fixture.write_text(json.dumps(dict(fixture_data, wait_for_idle=verb == 'start')))
                    run([sys.executable, __file__, '--turn', str(root), verb], env=env)
                    assert not list(directory.iterdir()), 'Worker did not clean up SSH sockets'
                    print('PASS: fresh worker ' + verb + ', automatic master/socket cleanup', flush=True)
                log.flush()
                log.seek(0)
                accepted = log.read().count('Accepted publickey')
                assert accepted == 2, f'Expected one authenticated connection per turn, got {accepted}'
                assert (source / 'human.txt').read_text() == 'preserve'
                assert (other / 'sibling').read_text() == 'preserve'
                print('PASS: two worker processes, persistent session, exactly one connection per turn')
            finally:
                server.terminate()
                server.wait(timeout=5)
                directory.rmdir()
                (other / 'sibling').unlink()
                other.rmdir()


if __name__ == '__main__':
    if sys.argv[1:2] == ['exec']:
        codex_double()
    elif sys.argv[1:2] == ['--probe']:
        probe()
    elif sys.argv[1:2] == ['--turn']:
        worker_turn(Path(sys.argv[2]), sys.argv[3])
    else:
        main()
