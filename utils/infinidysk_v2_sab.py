"""Keep the native download-client credential in its managed DUMB setting.

Adoption reads the authenticated running application, never a potentially stale
cold SQLite/WAL or PostgreSQL store. The stopped, task-owned import candidate is
the only database read here. No credential belongs in a status or receipt.
"""

import copy
import hmac
import json

from utils.infinidysk_v2_credentials import MAX_KEY, MAX_SAB_SETTINGS, _read_setting
from utils.infinidysk_migration_admission import INFINIDYSK_MIGRATION_ADMISSION_LOCK

SAB_ENV_NAME = "INFINIDYSK__SAB__API_KEY"


class NativeSabKeyError(ValueError):
    """A credential-free explanation of a refused configuration change."""


def _key(value, *, allow_empty=False):
    if allow_empty and value == "":
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_KEY
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise NativeSabKeyError("The native download-client API key needs review.")
    return value


def configured_sab_key(service):
    """Return the explicit leaf, refusing ambiguous aliases/section overrides."""
    environment = service.get("env") or {}
    if not isinstance(environment, dict):
        raise NativeSabKeyError("The native service environment needs review.")
    values = []
    for name, value in environment.items():
        if not isinstance(name, str) or not name.startswith("INFINIDYSK__"):
            continue
        if name.upper() == "INFINIDYSK__SAB":
            raise NativeSabKeyError(
                "Use individual native SAB environment settings instead of a whole SAB section override."
            )
        if name.upper() == SAB_ENV_NAME:
            values.append(_key(value, allow_empty=True))
    if len(set(values)) > 1:
        raise NativeSabKeyError(
            "Conflicting native download-client API-key environment aliases need review."
        )
    return values[0] if values else None


def canonical_sab_environment(service):
    """Keep one native leaf; an empty placeholder must not override native state."""
    environment = service.get("env") or {}
    if isinstance(environment, dict) and any(
        isinstance(name, str)
        and name.startswith("INFINIDYSK__")
        and name.upper() == "INFINIDYSK__SAB"
        for name in environment
    ):
        # Existing native whole-section overrides remain valid at launch. They
        # require review before automatic key management; never add a competing
        # leaf or change their precedence as a side effect of this integration.
        return dict(environment)
    key = configured_sab_key(service)
    environment = {
        name: value
        for name, value in environment.items()
        if not (
            isinstance(name, str)
            and name.startswith("INFINIDYSK__")
            and name.upper() == SAB_ENV_NAME
        )
    }
    if key is not None:
        environment[SAB_ENV_NAME] = key
    return environment


def _managed_environment(service, effective):
    effective = _key(effective)
    configured = configured_sab_key(service)
    if configured is not None and not hmac.compare_digest(
        configured.encode(), effective.encode()
    ):
        raise NativeSabKeyError(
            "The configured download-client key is not active yet. Restart InfiniDysk before reconnecting managed Arr clients."
        )
    environment = canonical_sab_environment(service)
    environment.pop("FRONTEND_BACKEND_API_KEY", None)
    environment[SAB_ENV_NAME] = configured or effective
    return environment


def adopt_running_sab_key(config_manager, service, effective, *, expected_service=None):
    """Persist the effective key once, without rotating it or saving stale config.

    Called after native readiness and outside migration/update recovery guards.
    Existing explicit configuration is authoritative and must already be active.
    """
    # Config API writes use this same lock. Keep the comparison, scoped save and
    # rollback together; native readiness/network I/O happens before this section.
    with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
        config = getattr(config_manager, "config", None)
        if (
            not isinstance(config, dict)
            or config.get("infinidysk") is not service
            or service.get("enabled") is not True
            or str(service.get("runtime_generation")) != "2"
            or (expected_service is not None and service != expected_service)
        ):
            raise NativeSabKeyError(
                "Native service configuration changed before key adoption."
            )
        environment = _managed_environment(service, effective)
        if environment != service.get("env"):
            previous = copy.deepcopy(service.get("env"))
            service["env"] = environment
            try:
                config_manager.save_config(service.get("process_name"))
            except Exception:
                if previous is None:
                    service.pop("env", None)
                else:
                    service["env"] = previous
                raise NativeSabKeyError(
                    "The native download-client key could not be saved; managed Arr clients were not changed."
                ) from None
        return environment[SAB_ENV_NAME]


def adopt_prepared_sab_key(service, database, *, require_stored_match=True):
    """Adopt only the accepted stopped native candidate before config exposure."""
    configured = configured_sab_key(service)
    if not require_stored_match and configured is not None:
        # Native deliberately omits environment-controlled paths from persisted
        # settings. A fresh install can use this explicit key with no stored key.
        # Major imports retain their independent stored-key continuity check.
        service["env"] = _managed_environment(service, configured)
        return
    raw = _read_setting(database, "config", "key", "value", "sab", MAX_SAB_SETTINGS)
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
        effective = value.get("api_key") if isinstance(value, dict) else None
    except ValueError:
        raise NativeSabKeyError(
            "The prepared native download-client API key could not be read."
        ) from None
    service["env"] = _managed_environment(service, effective)
