# Murphy

Start with `README.md` for the supported scope, configuration and commands.

- This is a standalone Python standard-library tool for Linux/WSL.
- Run `python3 tests/test_bridge.py` for local verification. Tests use temporary repositories and a CLI double; no real model or GitHub writes are needed.
- Keep repository/account identities and executable paths in local configuration. `config.example.json` must contain only fictional values.
- Never commit authentication files, installation configs, runtime databases, execution logs or session history.
- Preserve the durable queue, source revalidation, deduplication and explicit recovery of unknown outcomes.
- The current Codex adapter is read-only. Changes to execution permissions require explicit task scope and verification.
- Keep changes small; packaging, a writable executor, multi-repository dispatch and model routing are future work.
