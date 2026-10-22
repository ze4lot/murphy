#!/usr/bin/env python3
"""Build, inspect and install both candidate formats without GitHub/model access."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile

ROOT = Path(__file__).resolve().parent.parent
VERSION = '0.1.0'
MODULES = {'murphy/' + name for name in ('__init__.py', '__main__.py', 'auth.py', 'bridge.py', 'cli.py', 'manager.py', 'monitor.py')}
SOURCE = MODULES | {'README.md', 'LICENSE', 'CHANGELOG.md', 'RELEASE.md', 'bridge.py', 'config.example.json',
                    'pyproject.toml', 'MANIFEST.in', 'build_backend.py', 'setup.cfg'}
SOURCE |= {'tests/' + path.name for path in (ROOT / 'tests').glob('*.py')}
EGG_INFO = {'PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'entry_points.txt', 'top_level.txt'}


def run(args, cwd, env):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise AssertionError(f'Command failed: {args}\n{result.stdout}\n{result.stderr}')
    return result.stdout


def inspect(path):
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            files = {name: archive.read(name) for name in archive.namelist() if not name.endswith('/')}
        metadata = f'murphy_github-{VERSION}.dist-info/'
        allowed = MODULES | {metadata + name for name in ('LICENSE', 'METADATA', 'WHEEL', 'entry_points.txt',
                                                         'top_level.txt', 'RECORD')}
        example = f'murphy_github-{VERSION}.data/data/share/murphy/config.example.json'
        allowed.add(example)
        assert set(files) == allowed, (set(files) - allowed, allowed - set(files))
        assert files[metadata + 'LICENSE'] == (ROOT / 'LICENSE').read_bytes()
        assert b'Requires-Python: >=3.11' in files[metadata + 'METADATA']
        assert b'Requires-Dist:' not in files[metadata + 'METADATA']
    else:
        with tarfile.open(path, 'r:gz') as archive:
            files = {member.name.split('/', 1)[1]: archive.extractfile(member).read()
                     for member in archive.getmembers() if member.isfile()}
        allowed = SOURCE | {'PKG-INFO'} | {'murphy_github.egg-info/' + name for name in EGG_INFO}
        assert set(files) == allowed, (set(files) - allowed, allowed - set(files))
        assert files['LICENSE'] == (ROOT / 'LICENSE').read_bytes()
        example = 'config.example.json'
    assert files[example] == (ROOT / 'config.example.json').read_bytes()
    assert json.loads(files[example])['repository'] == 'example/project'
    for name, data in files.items():
        for forbidden in (os.fsencode(ROOT), os.fsencode(Path.home())):
            assert forbidden not in data, (name, forbidden)
    print('Archive contents PASS:', path.name)


def install(path, root, wheelhouse, env):
    target = root / ('wheel-env' if path.suffix == '.whl' else 'source-env')
    venv.EnvBuilder(with_pip=True).create(target)
    python = target / 'bin/python'
    install_env = dict(env, PATH=str(target / 'bin') + os.pathsep + env['PATH'])
    run([python, '-m', 'pip', '--isolated', 'install', '--no-index', '--find-links', wheelhouse,
         'setuptools==75.8.2', 'wheel==0.45.1'], root, install_env)
    run([python, '-m', 'pip', '--isolated', 'install', '--no-deps', '--no-build-isolation', '--no-index', path],
        root, install_env)
    home = root / (target.name + '-empty-home')
    home.mkdir()
    isolated = dict(install_env, HOME=str(home), GH_CONFIG_DIR=str(home / 'gh'), CODEX_HOME=str(home / 'codex'))
    cli = target / 'bin/murphy'
    assert 'projects' in run([cli, '--help'], root, isolated)
    assert run([cli, '--version'], root, isolated).strip() == 'murphy ' + VERSION
    assert 'projects' in run([python, '-I', '-m', 'murphy', '--help'], root, isolated)
    imported = json.loads(run([python, '-I', '-c',
        'import json,murphy,importlib.metadata as m;print(json.dumps([murphy.__file__,m.version("murphy-github")]))'],
        root, isolated))
    assert imported[0].startswith(str(target)) and imported[1] == VERSION, imported
    assert not list(home.iterdir()), 'Help/version must not create state or need configuration'
    assert (target / 'share/murphy/config.example.json').read_bytes() == (ROOT / 'config.example.json').read_bytes()
    missing = subprocess.run([str(cli), 'init', '--gh', '/missing/example-gh'], cwd=root, env=isolated,
                             capture_output=True, text=True, timeout=20)
    assert missing.returncode == 1 and 'Missing executable' in missing.stderr, missing.stderr
    # Exercise installed account setup and persistence through a fake gh executable.
    fake = root / ('fake-gh-' + target.name)
    fake.write_text('#!' + str(python) + '\nimport json\nprint(json.dumps('
                    '{"id":3001,"login":"example-owner","type":"User"}))\n')
    fake.chmod(0o700)
    assert 'example-owner' in run([cli, 'init', '--gh', fake, '--git', python, '--codex', python], root, isolated)
    status = json.loads(run([cli, 'status'], root, isolated))
    assert status == {'account': 'example-owner', 'paused': False, 'projects': [], 'commands': []}, status
    assert '-m, --monitor' in run([cli, 'run', '--help'], root, isolated)
    monitor = run([cli, 'monitor', '--once'], root, isolated)
    assert 'WYŁĄCZONY' in monitor and '\x1b' not in monitor, monitor
    monitored = subprocess.run([str(cli), 'run', '-m', '--once'], cwd=root, env=isolated,
                               capture_output=True, text=True, timeout=20)
    assert monitored.returncode == 1 and 'Connect a project' in monitored.stderr, monitored
    assert json.loads(run([cli, 'projects', 'list'], root, isolated)) == []
    single = json.loads(run([cli, 'auth', 'status'], root, isolated))
    assert single['mode'] == 'single' and single['owner'] == single['executor']
    # A separate account uses gh's own private configuration. Exercise installed
    # login/status/reauthentication and the installed Git helper with a CLI double.
    dual = root / ('two-account-gh-' + target.name)
    source = (ROOT / 'tests/fake_gh.py').read_text().split('\n', 1)[1]
    dual.write_text('#!' + str(python) + '\n' + source)
    dual.chmod(0o700)
    human = home / 'gh/user.json'
    human.parent.mkdir(parents=True, exist_ok=True)
    human.write_text(json.dumps({'id': 3001, 'login': 'example-owner', 'type': 'User'}))
    saved_human = human.read_bytes()
    separate = root / (target.name + '-separate-state')
    run([cli, '--state-dir', separate, 'init', '--separate-account', '--gh', dual, '--codex', python], root, isolated)
    status = json.loads(run([cli, '--state-dir', separate, 'auth', 'status'], root, isolated))
    assert status['mode'] == 'separate' and status['authenticated']
    assert status['owner']['id'] == 3001 and status['executor']['id'] == 2001
    helper_script = ('import json,sqlite3,subprocess,sys;from murphy.auth import environment;'
                     'db=sqlite3.connect(sys.argv[1]);'
                     's=json.loads(db.execute("SELECT value FROM settings WHERE key=\'account\'").fetchone()[0]);'
                     'r=subprocess.run(["git","credential","fill"],env=environment(s),'
                     'input="protocol=https\\nhost=github.com\\n\\n",text=True,capture_output=True);'
                     'assert r.returncode==0,r.stderr;assert "username=example-executor" in r.stdout')
    run([python, '-I', '-c', helper_script, separate / 'manager.sqlite'], root, isolated)
    executor_file = separate / 'auth/executor/user.json'
    executor_file.unlink()
    missing_login = subprocess.run([str(cli), '--state-dir', str(separate), 'auth', 'status'], cwd=root,
                                   env=isolated, capture_output=True, text=True, timeout=20)
    assert missing_login.returncode == 1 and not json.loads(missing_login.stdout)['authenticated']
    run([cli, '--state-dir', separate, 'auth', 'login'], root, isolated)
    assert json.loads(run([cli, '--state-dir', separate, 'auth', 'status'], root, isolated))['authenticated']
    assert human.read_bytes() == saved_human, 'Executor authentication changed the human gh configuration'
    print('Clean install/help/setup PASS:', path.name)


def main():
    with tempfile.TemporaryDirectory(prefix='murphy-package-') as temp:
        root = Path(temp)
        home = root / 'build-home'
        home.mkdir()
        # An explicit minimal environment excludes inherited tokens, credential
        # paths, Python imports and developer Git configuration.
        env = {'HOME': str(home), 'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8',
               'SOURCE_DATE_EPOCH': '315532800', 'PYTHONNOUSERSITE': '1',
               'PIP_CONFIG_FILE': os.devnull, 'PIP_DISABLE_PIP_VERSION_CHECK': '1'}
        outputs = []
        for name in ('first', 'second'):
            directory = root / name
            run([sys.executable, '-m', 'build', '--outdir', directory], ROOT, env)
            outputs.append({path.name: path for path in directory.iterdir()})
        assert set(outputs[0]) == {f'murphy_github-{VERSION}-py3-none-any.whl', f'murphy_github-{VERSION}.tar.gz'}
        assert set(outputs[0]) == set(outputs[1])
        for name, path in outputs[0].items():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest == hashlib.sha256(outputs[1][name].read_bytes()).hexdigest(), name + ' is not reproducible'
            print('Reproducible build PASS:', name, digest)
            inspect(path)
        wheelhouse = root / 'build-tools'
        run([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--no-deps', '--dest', wheelhouse,
             'setuptools==75.8.2', 'wheel==0.45.1'], root, env)
        for path in outputs[0].values():
            install(path, root, wheelhouse, env)
    print('Package verification PASS')


if __name__ == '__main__':
    main()
