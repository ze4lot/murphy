#!/usr/bin/env python3
"""Optional native Codex sandbox smoke check; no model calls or GitHub writes."""
import argparse
import http.server
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from murphy import bridge


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'network-ok')

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', default='codex')
    args = parser.parse_args()
    bridge.CONFIG = bridge.Config('example/project', 1001, 2001, 3001, 'example-owner')
    # Keep the sibling outside system temp, which the workspace profile permits writing.
    with tempfile.TemporaryDirectory(prefix='murphy-sandbox-check-', dir=Path.home()) as directory:
        root = Path(directory)
        source, task = root / 'source', root / 'task'
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=Test Bot',
                        '-c', 'user.email=bot@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture'], check=True)
        (source / 'human.txt').write_text('preserve')
        subprocess.run(['git', '-C', str(source), 'worktree', 'add', '-qb', 'codex/check', str(task)], check=True)
        subprocess.run(['git', '-C', str(task), 'remote', 'add', 'origin',
                        'https://github.com/example/project.git'], check=True)
        before = bridge.checkout(str(task))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        script = '''from pathlib import Path
import errno,subprocess,sys,urllib.request
Path('probe.txt').write_text('writable')
subprocess.run(['git','add','probe.txt'],check=True)
subprocess.run(['git','-c','user.name=Test Bot','-c','user.email=bot@example.invalid','commit','-qm','sandbox probe'],check=True)
assert urllib.request.urlopen(sys.argv[1],timeout=5).read()==b'network-ok'
try:
    Path(sys.argv[2]).write_text('wrong')
except OSError as error:
    assert error.errno in (errno.EACCES,errno.EPERM,errno.EROFS),error
else:
    raise AssertionError('Sibling source checkout unexpectedly writable')
print('PASS: worktree edit, commit, network access, sibling checkout protection')
'''
        try:
            subprocess.run([args.codex, 'sandbox', '--permission-profile', 'murphy',
                            *bridge.permission_args(before), '--cd', str(task), '--', sys.executable,
                            '-c', script, f'http://127.0.0.1:{server.server_port}', str(source / 'human.txt')],
                           check=True, timeout=30)
            assert (source / 'human.txt').read_text() == 'preserve'
            assert bridge.checkout(str(task))['head'] != before['head']
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    main()
