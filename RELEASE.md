# Preparing and promoting 0.1.0

This prepares candidate files for users. It does not publish them or authorize a merge, tag, deployment or real-model pilot.

## Verify and build

Use a clean checkout of the candidate commit and Linux/WSL with Python 3.11+:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'build==1.2.2.post1'
python3 tests/test_bridge.py
python3 tests/test_manager.py
.venv/bin/python tests/check_package.py
git diff --check
SOURCE_DATE_EPOCH=315532800 .venv/bin/python -m build
sha256sum dist/*
```

Build requirements are pinned in `pyproject.toml`. `SOURCE_DATE_EPOCH` fixes wheel timestamps; the backend normalizes source archive timestamps/owners/order and gzip headers. The package check repeats builds with that epoch and compares both artifact hashes. This establishes reproducibility for the tested interpreter/toolchain, not across all Python/zlib/platform versions.

Expected artifacts:

- `dist/murphy_github-0.1.0-py3-none-any.whl`: installable package.
- `dist/murphy_github-0.1.0.tar.gz`: source archive that can also be installed.

Both must contain the MIT license and fictional example configuration. Inspect the exact files before attaching them anywhere; private installation configuration, credentials, SQLite databases, execution logs, session history and machine-specific identities/paths must be absent. The checker enforces an explicit content allowlist. It installs each artifact outside the source tree in a fresh virtual environment with an empty HOME and no inherited credentials, and verifies CLI help/version, imports, example data and expected setup errors.

## Review and promotion

1. Open a Draft PR from the task branch to `develop`. Include candidate version, verification results, artifact hashes/build commands, changed usage and known limitations. Keep issue #2 open until authorized closure.
2. After review and explicit merge authorization, merge the implementation into `develop`.
3. If requested, perform the documented controlled pilot with the intended external tools/accounts. Record exact versions and measured outcomes; ordinary tests are not a live pilot.
4. With separate release authorization, prepare a promotion PR from `develop` to `master`, verify the exact candidate commit locally and review the changelog/artifacts. Task PRs and promotion PR updates do not run GitHub CI.
5. Once authorized, merge the promotion. This is the only event that runs GitHub CI: a merged same-repository `develop` → `master` PR. CI checks out the exact merge commit and runs the Python 3.11/3.12 test/build/install matrix. Closed-but-unmerged PRs and other source branches skip the job; ordinary pushes and manual dispatch do not trigger it.
6. After that CI passes, create a tag, publish a GitHub release or publish to a package index only when explicitly authorized. Recheck that the chosen distribution name is available before any package-index publication; no name reservation is claimed here.

No shared branch push, merge, tag or publication is part of preparing this candidate.
