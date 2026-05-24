# Contributing to Blackout Secure Discover Upstream Release

Thank you for your interest in contributing.

## Getting started

1. Fork the repository
2. Clone your fork:
   `git clone https://github.com/your-username/bos-discover-upstream-release.git`
3. Create a feature branch: `git checkout -b feat/your-feature`
4. (Optional) Create a venv: `python3 -m venv .venv && source .venv/bin/activate`
5. Install dev tooling: `pip install -r requirements-dev.txt`

The action itself is **stdlib-only**. The `requirements-dev.txt` file lists
only test / lint tooling — nothing it installs ships with the action.

## Development

### Run the test suite

```bash
pytest -q
```

### Lint

```bash
ruff check .
ruff format --check .
```

### Run the action locally

Set the same environment variables the composite action would set and run
the script directly:

```bash
GITHUB_OUTPUT=/tmp/gh-out \
SOURCE=github_release \
UPSTREAM_REPO=nginx/nginx \
STRIP_V_PREFIX=true \
TRACKER_PATH= \
python3 src/discover.py
```

## Pull request process

1. Add a test for any new behaviour (`test/`).
2. Run `pytest -q` and `ruff check .` locally.
3. Update `README.md` if you add or change an input/output.
4. Open the PR with a clear description of the change and the motivation.

## Code style

- Follow PEP 8; enforced by `ruff`.
- Keep the action **stdlib-only** — do not add runtime dependencies.
- Inputs flow through `env:`; never interpolate inputs into `run:` bodies.
- Each provider lives in a single function in `src/discover.py`; keep
  provider logic local rather than refactoring into shared helpers
  unless the duplication crosses three providers.

## Reporting issues

- Use GitHub Issues for bug reports.
- Include the `source:` value, sanitized inputs, and the failing run URL.
- For security issues, see [SECURITY.md](./SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under
the Apache License 2.0.
