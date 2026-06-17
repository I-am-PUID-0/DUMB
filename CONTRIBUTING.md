# Contributing to DUMB

Thanks for contributing to DUMB.

## Branch Model

- `dev` is the default collaboration branch.
- `master` is the production and release branch.
- Open normal feature and bugfix PRs to `dev`.

## Basic Workflow

1. Fork the repository.
2. Create a branch from `dev`.
3. Make focused changes with clear commit messages.
4. Run relevant checks before opening a PR.
5. Open your PR to `dev`.

## Local Checks

The devcontainer installs local Git hooks with `pre-commit` after dependencies are
installed. Commits and pushes from VS Code or a terminal inside the devcontainer
run `make verify` before Git accepts the operation.

To install the same hooks manually in an existing checkout:

```bash
poetry run pre-commit install
poetry run pre-commit install --hook-type pre-push
```

Run the lightweight backend checks before opening a PR when touching `api/`, `utils/`, or `tests/`:

```bash
make verify
```

If the Black formatting gate fails, apply the formatter and then re-run verification:

```bash
make format
make verify
```

`make verify` is intentionally check-only so it matches CI and pre-push behavior;
it reports formatting drift but does not rewrite files.

The underlying commands are:

```bash
poetry run python scripts/verify_project.py
poetry check --lock
poetry run black --check api utils tests scripts
poetry run ruff check api utils tests scripts
PYTHONPYCACHEPREFIX=/tmp/dumb-pycache poetry run python -m compileall -q api utils tests scripts
poetry run python -m unittest discover -s tests
```

`verify_project.py` checks project metadata, JSON config files, `.env.example` sync, workflow permissions, and test discovery scaffolding. Poetry lock consistency is checked before install. Black and Ruff are required gates. The temp pycache prefix avoids failures from root-owned `__pycache__` directories created by devcontainer/runtime processes.

When changing `utils/dumb_config.json`, regenerate `.env.example` before running verification:

```bash
make env-example
```

`make verify` checks that `.env.example` is current, but it does not rewrite the file for you. Use `make env-check` when you only want to check generated env-file drift.

Dependency scanning and dependency vulnerability auditing now live in one command:

```bash
make security
```

`make security` runs `pip-audit` and the local project secret scan script in `scripts/security_scan.py` (for obvious hard-coded secrets).
## Pull Request Expectations

- Use Conventional Commit style for PR titles and commits.
- Include a concise summary and testing notes.
- Link related issues.
- Add docs updates when behavior changes.

## Dependabot and CI Notes

- Dependabot updates are targeted to `dev`.
- Conventional commit checks run on PRs to `dev` and `master`.
- Lightweight Python CI runs project metadata checks, Black, Ruff, syntax compilation, and unit tests on PRs and pushes that touch backend code, tests, dependency metadata, or the CI workflow.
- Release automation remains pinned to `master`.

## Full Contributor Guide

For full guidance, see:

- <https://dumbarr.com/contributing>
