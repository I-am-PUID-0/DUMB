# Repository Guidelines for Agents

## Project purpose and runtime model

DUMB (Debrid Unlimited Media Bridge) is an all-in-one Docker-oriented media automation stack. The Python backend coordinates a FastAPI API, service process lifecycle management, configuration generation/migration, update checks, logs, metrics, authentication, and integrations for bundled services such as Arr apps, Plex/Jellyfin/Emby, Decypharr, NzbDAV, Riven, Traefik, cloudflared, PostgreSQL, and related tools.

The container runtime expects persistent paths such as `/config`, `/log`, `/data`, `/mnt/debrid`, and `/healthcheck`. Many modules intentionally use these absolute paths, so local tests often rely on fallback paths or mocks rather than a fully running container.

## Repository layout

- `main.py` is the runtime orchestrator. It loads the global config, starts FastAPI, applies migrations/port reservations, starts configured services, schedules background workers, and coordinates health/update loops.
- `api/api_service.py` builds the FastAPI app and wires routers under `/auth`, `/process`, `/config`, `/health`, `/logs`, `/metrics`, `/seerr-sync`, and `/ws`.
- `api/routers/` contains HTTP and WebSocket endpoints. `api/routers/process.py` is large and owns service lifecycle, updates, symlink backup/restore, core/optional service metadata, and capability endpoints.
- `api/api_state.py` owns runtime API state including service status cache, update status/notices, symlink job status, and persisted notice files.
- `utils/config_loader.py` is the central config manager. It merges `/config/.env`, validates against `utils/dumb_config_schema.json`, fills defaults from `utils/dumb_config.json`, prunes unknown keys, backs up changed config, and writes atomically.
- `utils/processes.py` owns subprocess setup/start/stop/restart, process registration, logs, auto-restart state, and `/healthcheck/running_processes.json` updates.
- `utils/*_settings.py` modules contain service-specific setup/configuration logic. Prefer adding service-specific behavior there instead of expanding routers or `main.py` unless orchestration is truly required.
- `utils/dependency_map.py` and `utils/core_services.py` define service dependency/core-service behavior used by startup and API views.
- `utils/dumb_config.json` is the source of default service configuration. `utils/dumb_config_schema.json` must permit any default/config shape you introduce.
- `scripts/` contains maintenance checks: project metadata validation, `.env.example` generation, and local secret scanning.
- `tests/` uses `unittest` and includes focused tests for security, config/schema behavior, routers, process manifests, metrics, settings helpers, and utility modules.


## Companion repositories and cross-repo changes

- DUMB backend/container repo: this repository is the Python orchestration backend and Docker image source of truth. Backend API, config defaults, process lifecycle, and service integration changes belong here.
- DUMB frontend repo: `https://github.com/nicocapalbo/dmbdb` (`repo_owner: nicocapalbo`, `repo_name: dmbdb` in `dumb.frontend`) provides the dashboard/web UI that talks to this backend. If you change API routes, response shapes, auth behavior, WebSocket contracts, onboarding/config schema semantics, service UI metadata, or capability fields, check whether a coordinated frontend change is required. Do not assume backend-only compatibility for user-visible API changes.
- DUMB docs repo: `https://github.com/I-am-PUID-0/DUMB_docs` backs the public documentation at `dumbarr.com`. If you add/remove services, change setup steps, ports, config keys/defaults, environment variables, API-visible behavior, screenshots, or operational guidance, prepare a matching docs update there or explicitly call out the docs follow-up in the PR.
- Cross-repo compatibility: prefer additive API/config changes when possible. If a breaking change is unavoidable, document the migration path, update defaults/schema/tests here, coordinate frontend/docs updates, and make the PR description clear about the required release ordering.

## Development commands

Use Poetry-managed commands unless a task is explicitly independent of dependencies.

- Install dependencies when needed: `poetry install`
- Run the full verification gate: `make verify`
- Run unit tests only: `make test`
- Run metadata/config/env/workflow checks: `make metadata`
- Check lockfile consistency: `make lock-check`
- Format Python files: `make format`
- Check formatting: `make format-check`
- Run Ruff: `make lint`
- Compile Python syntax: `make syntax`
- Run dependency audit and secret scan: `make security`
- Regenerate `.env.example` after config default changes: `make env-example`
- Check `.env.example` drift without rewriting: `make env-check`

`make verify` currently runs metadata, lock check, Black check, Ruff, compileall with `PYTHONPYCACHEPREFIX=/tmp/dumb-pycache`, unit tests, and security checks. If you cannot run the full gate, run the smallest relevant subset and explain why.

## Style and tooling expectations

- Python target is `>=3.11,<4.0`.
- Black line length is 88. Ruff line length is 88 and currently enforces a focused correctness/security rule set.
- Do not introduce try/except blocks around imports.
- Keep imports and code compatible with the existing style; this repository commonly uses direct imports and module-level singletons.
- Prefer `pathlib` in new maintenance scripts/tests, but respect surrounding style in existing modules.
- Avoid broad rewrites in large files such as `api/routers/process.py`; make tightly scoped changes and add helper functions where possible.
- Do not commit generated caches, local config, logs, runtime data, or virtual environments.

## Configuration and environment rules

- Treat `utils/dumb_config.json` as the default config source and `utils/dumb_config_schema.json` as the validation contract. Changes to one usually require changes to the other.
- Any change to config defaults must be reflected in `.env.example` by running `make env-example`.
- Environment variable names are generated from nested config paths using uppercase underscore-separated keys. The generator lives in `scripts/generate_env_example.py`.
- `ConfigManager` intentionally prunes keys that are not present in the default config shape. When adding new settings, add them to the default config before relying on persistence.
- Config writes should be atomic or use existing `ConfigManager.save_config()`/helper behavior. Avoid direct ad-hoc writes to `/config/dumb_config.json`.
- Be careful with migrations in `utils/config_loader.py` and `main.py`; they may run automatically on user configs at container startup.

## Service and process management rules

- For new managed services, update all relevant places consistently: default config, schema, dependency map/core service metadata if applicable, setup/settings module, process lifecycle/API surface if needed, docs/env example, and tests.
- Services can be single-instance or `instances` based. Preserve this distinction because config merge/prune logic treats `instances` as a templated shape.
- `ProcessHandler` prefixes process names in contexts and tracks setup, subprocess loggers, external processes, auto-restart, and healthcheck state. Avoid bypassing it for long-running managed services.
- Startup may call setup/configure before launching a process. Service-specific setup should live in `utils/*_settings.py` or `utils/setup.py` patterns rather than in endpoint handlers.
- Healthcheck integrations depend on `/healthcheck/running_processes.json`; if changing lifecycle behavior, ensure registration/unregistration remains accurate.
- Port handling in `main.py` reserves enabled service ports and may auto-shift conflicts. Preserve explicit user intent where possible and persist only when necessary.

## API, auth, and security rules

- FastAPI app creation is centralized in `api/api_service.py`; include new routers there and add tests for new endpoints.
- Auth utilities and user management live in `utils/auth.py`, `utils/auth_config.py`, `utils/user_management.py`, and `api/routers/auth.py`. Avoid logging tokens/passwords/secrets.
- Use existing redaction helpers in `utils/logger.py`/process logging when handling subprocess output or user-provided values.
- Network-facing URL behavior should use `utils/url_security.py` safeguards where applicable; tests cover SSRF/security-sensitive behavior.
- CORS intentionally ignores wildcard origins for credentialed requests. Do not weaken this without explicit security review.
- Run `make security` (or at least `poetry run python scripts/security_scan.py`) when touching auth, logging, config, URL/network, or secret-handling code.

## Testing guidance

- Tests are discovered with `poetry run python -m unittest discover -s tests`; keep `tests/__init__.py` in place.
- Add or update tests close to the affected behavior. Existing test file names indicate the preferred scope for many modules.
- For config changes, run at minimum `make metadata`, `make env-check`, and the relevant config/schema tests.
- For router changes, exercise helper functions directly where possible and use FastAPI test clients only where endpoint integration matters.
- For process/update/symlink changes, avoid tests that start real external services; mock subprocesses, filesystem paths, and network calls.
- Use `PYTHONPYCACHEPREFIX=/tmp/dumb-pycache` for compile checks to avoid permission problems from root-owned runtime caches.

## Documentation and release notes

- Update `README.md`, `CONTRIBUTING.md`, or docs-facing generated files when behavior, setup, ports, config, or user-visible APIs change.
- Conventional Commit style is expected for commits and PR titles.
- Normal feature/bugfix PRs target `dev`; `master` is production/release.
- Keep `pyproject.toml` version and `.github/.release-please-manifest.json` aligned if a task explicitly changes the project version.

## Operational cautions

- This project manages real media-server processes, databases, mounts, tunnels, and credentials. Be conservative with defaults and migrations.
- Do not hard-code real tokens, passwords, API keys, hostnames, or private paths. Use placeholders and ensure the secret scan passes.
- Avoid changing public ports, default paths, process names, or config keys unless the change includes compatibility handling and tests.
- Preserve existing container assumptions unless intentionally migrating them: `/config`, `/log`, `/data`, `/mnt/debrid`, `/healthcheck`, `/utils`, and `/healthcheck/running_processes.json` are part of the runtime contract.
