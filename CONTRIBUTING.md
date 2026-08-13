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

The devcontainer does not assign a maintainer identity. On create/attach it keeps
an existing repository-local identity, or copies a complete global identity into
the bind-mounted checkout so it survives container rebuilds. For a new checkout,
configure your own identity once with `git config --local user.name` and
`git config --local user.email`, or provide both `GIT_USER_NAME` and
`GIT_USER_EMAIL` as explicit devcontainer environment values.

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

## Disposable Service Update Regressions

Updater changes can be exercised without downgrading a live DUMB deployment. The
retained regression suite creates a fresh container, `/config`, and `/data` tree
for every case; installs the configured previous release; updates it through the
real DUMB API; verifies application health; and removes the disposable state.

Build the local regression image first with `make regression-image`. It pulls
and extends the current published DUMB development image with the dependencies
needed by `main.py` and tags it `dumb-regression-base:local`. Each worker then
bind-mounts the current checkout's `main.py`, `api/`, and `utils/`, so the tested
service lifecycle code is the latest working tree rather than the older code
baked into that base image. Cases are defined in
`scripts/service_update_regression_matrix.json`. Qualified cases run by default,
while pending entries document services that still need deterministic versions,
dependencies, or safe credential fixtures.

Run two cases concurrently (the recommended default for a development machine):

```bash
python scripts/regression_service_updates.py --jobs 2
```

The equivalent Make target is `make regression-service-updates`; override
`REGRESSION_JOBS` and pass extra suite arguments through `REGRESSION_ARGS`.
Run this Docker-backed target from a host/workbench shell with Docker access,
not from a devcontainer that lacks the Docker socket.

Run selected cases or deliberately qualify pending coverage:

```bash
python scripts/regression_service_updates.py --jobs 2 \
  --case pulsarr-release --case nzbdav-release
python scripts/regression_service_updates.py --jobs 1 \
  --case nzbdav-prebuilt-rc
python scripts/regression_service_updates.py --jobs 2 \
  --include-pending --case tautulli-release
```

Anonymous GitHub API quotas are small and parallel source tests consume them
quickly. The harness fails fast instead of sleeping until a distant quota reset.
When explicitly appropriate on a trusted development machine, add
`--use-gh-auth-token`; the runner reads the current `gh` CLI token without
printing it, places it only in each disposable private config, and deletes that
config during normal cleanup.

Each worker uses isolated service state and a shared verified install cache under
the workspace parent. Reports and per-case stdout/stderr are written beneath
`.regression-reports/` and are ignored by Git. Keep concurrency bounded: source
builds such as Seerr, InfiniDysk, and Traefik Proxy Admin can consume substantial CPU,
memory, network bandwidth, and temporary storage.

The `nzbdav-prebuilt-rc` case starts an older fixed RC, changes the saved target
to DUMB's `prerelease` selector through the API, and verifies that the
architecture-specific archive update leaves both the frontend and backend
healthy. Matrix cases with `target_version` use the configured-target update
path by default; `update_target: channel` exercises a moving release channel,
and cases without either continue to exercise **Override + latest**.

Maintainers can also dispatch the **Service Update Regression** workflow. It
runs the qualified matrix independently on the native X64 and ARM64 self-hosted
runner pools and retains each architecture's report as a workflow artifact.
Credential-free install-only cases (currently Zurg) validate downloadable
runtime artifacts without contacting a provider account; provider-backed health
tests require a deliberately supplied disposable credential and are not part of
the default workflow.

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
