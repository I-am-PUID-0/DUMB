"""Application boundary for setup plans; no work runs at module import time."""

from __future__ import annotations

import copy
import json
import os
import stat
from dataclasses import asdict
from pathlib import Path

import defusedxml.ElementTree as ET

from utils.arrangement_http import ApiClient, ApiError
from utils.arrangement_inputs import resolve_provider_inputs
from utils.arrangement_plan import resource_config


def _result(execution, verification="not_checked", reason="", **extra):
    return {
        "execution": execution,
        "verification": verification,
        "reason": reason,
        **extra,
    }


def _arr_api(config):
    path = config.get("config_file")
    if not path or not os.path.isfile(path):
        return None
    try:
        document = ET.parse(path).getroot()
        key = (document.findtext("ApiKey") or "").strip()
        base = (document.findtext("UrlBase") or "").strip().rstrip("/")
        if not key or (
            base
            and (
                not base.startswith("/")
                or base.startswith("//")
                or any(c in base for c in "?#\\\r\n")
            )
        ):
            return None
        return ApiClient(
            f"http://127.0.0.1:{int(config['port'])}{base}",
            headers={"X-Api-Key": key},
            managed=True,
        )
    except (OSError, ET.ParseError, ValueError, KeyError):
        return None


class ArrangementRuntime:
    def __init__(
        self,
        manager,
        updater,
        api_state,
        logger,
        process_handler=None,
        *,
        ownership_directory="/config/.dumb-setup/connections",
    ):
        self.manager = manager
        self.updater = updater
        self.api_state = api_state
        self.logger = logger
        self.process_handler = process_handler
        self.ownership_directory = ownership_directory
        self.last_integrations = []
        self.cancel_check = lambda identifier: False

    def defaults(self):
        with open(self.manager.default_config_path) as handle:
            return json.load(handle)

    def start(self, payload):
        from api.routers.process import UnifiedStartRequest, _run_startup
        from utils.arrangement_plan import project_configuration
        from utils.arrangement_providers import validate_connection_budgets

        validate_connection_budgets(resolve_provider_inputs(payload))
        from utils.arrangement_infinidysk import transition

        proposed, resources = project_configuration(
            payload, self.manager.config, self.defaults()
        )
        if any(
            item["service_key"] == "infinidysk" for item in resources
        ) and transition(
            self.manager.config.get("infinidysk") or {},
            proposed.get("infinidysk") or {},
        ):
            raise ValueError(
                "Complete the guarded InfiniDysk 2 wizard and create a fresh setup plan before starting services."
            )
        requested = copy.deepcopy(payload)
        retry_resources = requested.pop("_retry_resources", None)
        values = (requested.pop("arrangement", {}) or {}).get("values", {})
        _, resources = project_configuration(
            requested, self.manager.config, self.defaults()
        )
        cores = requested.get("core_services") or []
        cores = [cores] if isinstance(cores, dict) else cores
        if retry_resources is not None:
            allowed = set(retry_resources)
            primary_ids = {resource["id"] for resource in resources}
            for identifier in sorted(allowed - primary_ids):
                dependency = resource_config(self.manager.config, identifier)
                if dependency.get("enabled") and dependency.get("process_name"):
                    self.updater.auto_update(
                        dependency["process_name"], enable_update=False
                    )
            allowed_resources = {r["id"]: r for r in resources if r["id"] in allowed}
            cores = [
                core
                for core in cores
                if any(
                    r["service_key"] == core["name"]
                    and (
                        not r.get("instance_key")
                        or core.get("instance_name", r["instance_key"])
                        == r["instance_key"]
                    )
                    for r in allowed_resources.values()
                )
            ]
            requested["optional_services"] = [
                key for key in requested.get("optional_services", []) if key in allowed
            ]
            resources = list(allowed_resources.values())
        for resource in resources:
            rid, key = resource["id"], resource["service_key"]
            choices = {
                field: values[f"{rid}::{field}"]
                for field in ("arr_instances", "battery_mode", "use_phalanx_db")
                if f"{rid}::{field}" in values
                and (
                    field != "arr_instances"
                    or key in {"prowlarr", "neutarr", "profilarr"}
                )
            }
            if key == "cli_debrid" and not choices.get("battery_mode"):
                from utils.cli_source_capabilities import effective_battery_mode

                proposed, _ = project_configuration(
                    payload, self.manager.config, self.defaults()
                )
                detected_mode = effective_battery_mode(resource_config(proposed, rid))
                if detected_mode is None:
                    raise ValueError(
                        "Choose the CLI source's Battery topology before starting."
                    )
                choices["battery_mode"] = detected_mode
            for core in cores:
                if core["name"] == key and (
                    not resource.get("instance_key")
                    or core.get("instance_name", resource["instance_key"])
                    == resource["instance_key"]
                ):
                    core.setdefault("service_options", {}).setdefault(key, {}).update(
                        choices
                    )
            if key in requested.get("optional_services", []):
                requested.setdefault("optional_service_options", {}).setdefault(
                    key, {}
                ).update(choices)
            if key == "pulsarr" and values.get(f"{rid}::tmdb_token"):
                self.manager.config[key].setdefault("env", {})["tmdbApiKey"] = values[
                    f"{rid}::tmdb_token"
                ]
                self.manager.save_config()
        for core in cores:
            if (
                core.get("name") == "infinidysk"
                and str(
                    (self.manager.config.get("infinidysk") or {}).get(
                        "runtime_generation"
                    )
                    or "1"
                )
                == "2"
            ):
                # The guarded native workflow owns dependent mount identity.
                # Keep only the service's validated options; stale Step4 mount
                # drafts cannot reconfigure an already accepted native owner.
                options = core.get("service_options") or {}
                core["service_options"] = {
                    key: value for key, value in options.items() if key == "infinidysk"
                }
        requested["core_services"] = cores
        if not cores and not requested.get("optional_services"):
            return {"results": [], "errors": [], "integration_results": []}
        from utils.integration_result import integration_targets

        with integration_targets(
            {
                r["id"]
                for r in resources
                if r["service_key"] in {"radarr", "sonarr", "lidarr", "whisparr"}
            }
        ):
            result = _run_startup(
                UnifiedStartRequest.model_validate(requested),
                self.updater,
                self.api_state,
                self.logger,
            )
        self.last_integrations = result.get("integration_results", [])
        return result

    def schema_fields(self, resource, config, payload):
        from utils.arrangement_connections import schema_fields as connection_fields
        from utils.arrangement_app_media import schema_fields as app_media_fields
        from utils.arrangement_helpers import schema_fields as helper_fields
        from utils.arrangement_media import schema_fields as media_fields
        from utils.arrangement_providers import schema_fields as provider_fields
        from utils.arrangement_external import global_fields
        from utils.arrangement_consumer import schema_fields as consumer_fields
        from utils.arrangement_indexers import schema_fields as indexer_fields
        from utils.arrangement_profiles import schema_fields as profile_fields
        from utils.arrangement_streams import schema_fields as streams_fields
        from utils.arrangement_pgadmin import schema_fields as pgadmin_fields
        from utils.arrangement_preferences import schema_fields as preferences_fields
        from utils.arrangement_workflow import schema_fields as workflow_fields
        from utils.arrangement_discovery import schema_fields as discovery_fields
        from utils.arrangement_mediastorm import schema_fields as mediastorm_fields
        from utils.arrangement_arr_providers import schema_fields as arr_provider_fields
        from utils.arrangement_altmount_account import schema_fields as altmount_fields
        from utils.arrangement_proxy_routes import schema_fields as proxy_route_fields
        from utils.arrangement_riven import schema_fields as riven_fields
        from utils.arrangement_plan import public_schema

        if resource["id"] == "setup":
            return public_schema(global_fields(config, payload))
        if (
            resource["service_key"] == "infinidysk"
            and str(
                resource_config(config, resource["id"]).get("runtime_generation") or "1"
            )
            == "2"
        ):
            from utils.arrangement_infinidysk import choice_fields

            return public_schema(
                choice_fields(
                    resource,
                    self.manager.config.get("infinidysk") or {},
                    resource_config(config, resource["id"]),
                )
            )

        fields = (
            helper_fields(resource, config, payload)
            + app_media_fields(resource, config, payload)
            + connection_fields(resource, config, payload)
            + media_fields(resource, config, payload)
            + provider_fields(resource, config, payload)
            + consumer_fields(resource, config, payload)
            + indexer_fields(resource, config, payload)
            + profile_fields(resource, config, payload)
            + streams_fields(resource, config, payload)
            + pgadmin_fields(resource, config, payload)
            + preferences_fields(resource, config, payload)
            + workflow_fields(resource, config, payload)
            + discovery_fields(resource, config, payload)
            + mediastorm_fields(resource, config, payload)
            + arr_provider_fields(resource, config, payload)
            + altmount_fields(resource, config, payload)
            + proxy_route_fields(resource, config, payload)
            + riven_fields(resource, config, payload)
        )
        # Discovery is read-only and only attempted for an already running app;
        # a fresh installation must not block on APIs that do not exist yet.
        cfg = resource_config(config, resource["id"])
        if (
            resource.get("external")
            or self.api_state.get_status(cfg.get("process_name", "")) == "running"
        ):
            from utils.arrangement_connections import discover_fields

            fields += discover_fields(resource, config, payload)
            from utils.arrangement_indexers import discover_fields as indexer_discovery
            from utils.arrangement_profiles import discover_fields as profile_discovery

            fields += indexer_discovery(resource, config, payload)
            fields += profile_discovery(resource, config, payload)
            from utils.arrangement_preferences import (
                discover_fields as preference_discovery,
            )

            fields += preference_discovery(resource, config, payload)
            from utils.arrangement_arr_providers import (
                discover_fields as arr_provider_discovery,
            )

            fields += arr_provider_discovery(resource, config, payload)
            from utils.arrangement_proxy_routes import (
                discover_fields as proxy_discovery,
            )

            fields += proxy_discovery(resource, config, payload)
        return public_schema(fields)

    def inspect(self, action, payload, operation="recheck"):
        return self._normalise_outcome(
            action, self._dispatch(action, payload, operation, read_only=True)
        )

    def apply(self, action, payload, operation="apply"):
        if operation == "start":
            operation = "apply"
        return self._normalise_outcome(
            action, self._dispatch(action, payload, operation, read_only=False)
        )

    @staticmethod
    def _normalise_outcome(action, result):
        if not isinstance(result, dict):
            return result
        if result.get("execution") == "unsupported":
            return {
                **result,
                "automation": result.get("automation", "unknown"),
                "execution": "waiting_input",
                "retryable": False,
            }
        if (
            "automation" not in result
            and action.get("declared_automation")
            and result.get("execution") in {"planned", "applied", "already_present"}
            and result.get("verification") != "failed"
        ):
            result = {**result, "automation": action["declared_automation"]}
        return result

    def _dispatch(self, action, payload, operation, *, read_only):
        from utils.arrangement_infinidysk import (
            manual_outcome,
            transition,
            uses_legacy_adapter,
        )
        from utils.arrangement_plan import project_configuration

        proposed, resources = project_configuration(
            payload, self.manager.config, self.manager.config
        )
        pending = any(
            item["service_key"] == "infinidysk" for item in resources
        ) and transition(
            self.manager.config.get("infinidysk") or {},
            proposed.get("infinidysk") or {},
        )
        if pending:
            return manual_outcome(
                "Complete the guarded InfiniDysk 2 wizard and create a fresh setup plan; ordinary setup will not change services."
            )
        if action.get("action") == "native_transition":
            return manual_outcome(
                "The native topology changed outside this setup plan. Create a fresh plan after the guarded wizard finishes."
            )
        if uses_legacy_adapter(action, proposed, payload):
            return manual_outcome()
        from utils.arrangement_connections import apply_connection, inspect_connection
        from utils.arrangement_app_media import apply_app_media, inspect_app_media
        from utils.arrangement_helpers import apply_helper, inspect_helper
        from utils.arrangement_media import apply_media, inspect_media
        from utils.arrangement_providers import apply_provider, inspect_provider

        config = self.manager.config
        if action["service_key"] == "riven_backend" and action["action"] in {
            "backend_connection",
            "media_connection",
        }:
            from utils.arrangement_riven import inspect_riven, apply_riven

            return (inspect_riven if read_only else apply_riven)(
                action, config, payload, operation
            )
        if action["service_key"] == "traefik_proxy_admin" and action["action"] in {
            "account",
            "proxy_routes",
        }:
            from utils.arrangement_proxy_routes import (
                inspect_proxy_routes,
                apply_proxy_routes,
            )

            return (inspect_proxy_routes if read_only else apply_proxy_routes)(
                action,
                config,
                payload,
                operation,
                ownership_directory=self.ownership_directory,
            )
        if action["service_key"] == "altmount" and action["action"] in {
            "account",
            "webdav",
        }:
            from utils.arrangement_altmount_account import (
                inspect_altmount_account,
                apply_altmount_account,
            )

            if not read_only and action["action"] == "webdav":
                before = inspect_altmount_account(action, config, payload)
                if (before.get("evidence") or {}).get("controlled_restart_required"):
                    from utils.arrangement_control import configure_altmount_webdav

                    return configure_altmount_webdav(
                        action,
                        config,
                        payload,
                        self.process_handler,
                        self.updater,
                        inspect_altmount_account,
                        apply_altmount_account,
                    )
            return (inspect_altmount_account if read_only else apply_altmount_account)(
                action, config, payload, operation
            )
        if action["service_key"] == "mediastorm" and action["action"] in {
            "account",
            "sources",
            "metadata",
        }:
            from utils.arrangement_mediastorm import (
                inspect_mediastorm,
                apply_mediastorm,
            )

            return (inspect_mediastorm if read_only else apply_mediastorm)(
                action, config, payload, operation
            )
        if action["action"] == "workflow_test":
            from utils.arrangement_workflow import workflow_action

            return workflow_action(
                action,
                config,
                payload,
                operation,
                read_only=read_only,
                directory=str(Path(self.ownership_directory).parent / "workflows"),
                cancelled=lambda: self.cancel_check(action.get("_setup_run_id")),
            )
        if action["action"] in {"arr_preferences", "subtitle_providers"}:
            from utils.arrangement_preferences import (
                inspect_preferences,
                apply_preferences,
            )

            return (inspect_preferences if read_only else apply_preferences)(
                action,
                config,
                payload,
                operation,
                ownership_directory=self.ownership_directory,
            )
        if action["action"] in {
            "companion_connection",
            "discovery",
            "ingestion",
            "library_hook",
            "public_route",
        }:
            from utils.arrangement_discovery import inspect_discovery, apply_discovery

            if not read_only and action["action"] in {
                "companion_connection",
                "library_hook",
            }:
                from utils.arrangement_control import configure_discovery

                return configure_discovery(
                    action,
                    config,
                    payload,
                    self.process_handler,
                    self.updater,
                    inspect_discovery,
                    apply_discovery,
                    manager=self.manager,
                )
            return (inspect_discovery if read_only else apply_discovery)(
                action, config, payload, operation
            )
        if action["action"] == "external_registry":
            from utils.arrangement_external import external_resources

            selected = external_resources(payload)
            return _result(
                "already_present" if selected else "declined",
                "unavailable",
                "The external registry choices are retained privately. Each server has a separate authenticated readiness action.",
                evidence={
                    "level": "accepted_routing_choice",
                    "resources": [r["id"] for r in selected],
                },
            )
        if action["action"] == "external_ready":
            from utils.arrangement_external import inspect_external, apply_external

            return (inspect_external if read_only else apply_external)(
                action, config, payload, operation
            )
        if action["action"] == "consumer_paths":
            from utils.arrangement_consumer import verify_consumer

            return verify_consumer(
                action,
                config,
                payload,
                operation,
                process_handler=self.process_handler,
                read_only=read_only,
                cancelled=lambda: self.cancel_check(action.get("_setup_run_id")),
            )
        if action["action"] == "indexers":
            from utils.arrangement_indexers import inspect_indexers, apply_indexers

            return (inspect_indexers if read_only else apply_indexers)(
                action,
                config,
                payload,
                operation,
                ownership_directory=self.ownership_directory,
            )
        if action["service_key"] == "profilarr" and action["action"] == "profiles":
            from utils.arrangement_profiles import inspect_profiles, apply_profiles

            return (inspect_profiles if read_only else apply_profiles)(
                action,
                config,
                payload,
                operation,
                ownership_directory=self.ownership_directory,
            )
        if action["service_key"] == "aiostreams" and action["action"] in {
            "backend_presets",
            "public_origin",
        }:
            from utils.arrangement_streams import inspect_streams, apply_streams

            return (inspect_streams if read_only else apply_streams)(
                action, config, payload, operation
            )
        if (
            action["service_key"] == "pgadmin"
            and action["action"] == "postgres_connection"
        ):
            from utils.arrangement_pgadmin import inspect_pgadmin, apply_pgadmin

            return (inspect_pgadmin if read_only else apply_pgadmin)(
                action, config, payload, operation
            )
        if action["action"] == "nntp":
            try:
                payload = resolve_provider_inputs(payload, strict=True)
            except ValueError:
                return _result(
                    "waiting_input",
                    "not_checked",
                    "Choose a valid shared NNTP account source and enter its credentials before applying.",
                    retryable=False,
                )
        if action["action"] == "runtime":
            return self._runtime(action, config)
        if action["action"].startswith("provider."):
            if action["source"].startswith("external_arr."):
                from utils.arrangement_arr_providers import (
                    inspect_arr_provider,
                    apply_arr_provider,
                )

                from utils.arrangement_restart import reconcile_with_restart

                return reconcile_with_restart(
                    action,
                    config,
                    payload,
                    operation,
                    lambda: (inspect_arr_provider if read_only else apply_arr_provider)(
                        action,
                        config,
                        payload,
                        operation,
                        ownership_directory=self.ownership_directory,
                    ),
                    process_handler=self.process_handler,
                    updater=self.updater,
                    read_only=read_only,
                )
            return self._provider_link(action, payload, read_only=read_only)
        if action["action"] == "arr_connections":
            return self._assign_arr_targets(action, payload, read_only=read_only)
        if action["action"] == "arr_connection":
            from utils.arrangement_links import inspect_link, apply_link

            return (inspect_link if read_only else apply_link)(
                action,
                config,
                payload,
                operation,
                manager=self.manager,
                ownership_directory=self.ownership_directory,
            )
        if action["service_key"] == "plex" and action["action"] == "account":
            from utils.arrangement_plex_claim import inspect_claim, apply_claim

            return (inspect_claim if read_only else apply_claim)(
                action, config, payload
            )
        if (
            action["service_key"] == "tautulli"
            and action["action"] == "backend_connection"
            and not read_only
        ):
            from utils.arrangement_control import configure_tautulli

            return configure_tautulli(
                action,
                config,
                payload,
                self.process_handler,
                self.updater,
                inspect_media,
                apply_media,
            )
        adapters = (
            (
                inspect_provider,
                inspect_media,
                inspect_helper,
                inspect_app_media,
                inspect_connection,
            )
            if read_only
            else (
                apply_provider,
                apply_media,
                apply_helper,
                apply_app_media,
                apply_connection,
            )
        )
        for adapter in adapters:
            if adapter in (
                apply_connection,
                inspect_connection,
                apply_app_media,
                inspect_app_media,
            ):
                result = adapter(
                    action,
                    config,
                    payload,
                    operation,
                    ownership_directory=self.ownership_directory,
                )
            elif adapter == apply_helper:
                result = adapter(
                    action, config, payload, operation, manager=self.manager
                )
            else:
                result = adapter(action, config, payload, operation)
            if result is not None:
                if action["action"] == "protection" and action["service_key"] in {
                    "jellyfin",
                    "emby",
                }:
                    result = self._protection_policy(
                        action, payload, result, read_only=read_only
                    )
                return result
        if action["action"] == "metadata" and action["service_key"] == "pulsarr":
            return self._pulsarr_metadata(action, payload, read_only=read_only)
        if action["action"] == "bootstrap" and action["service_key"] == "authelia":
            cfg = config.get("authelia", {})
            if cfg.get("public_url") and cfg.get("cookie_domain"):
                return _result(
                    "already_present",
                    "not_checked",
                    "Managed bootstrap settings exist. Open Authelia setup to verify portal access and factor enrollment.",
                )
        return _result(
            "waiting_input",
            "unavailable",
            action.get("instructions")
            or "Complete this task in the application, then recheck or explicitly confirm the manual step.",
            retryable=False,
        )

    def _assign_arr_targets(self, action, payload, *, read_only):
        values = (payload.get("arrangement") or {}).get("values") or {}
        selected = values.get(f"{action['source']}::arr_instances")
        cfg = resource_config(self.manager.config, action["source"])
        if selected is None and action["service_key"] in {
            "prowlarr",
            "neutarr",
            "profilarr",
        }:
            selected = cfg.get("arr_instances")
        if not isinstance(selected, list):
            return _result(
                "waiting_input",
                "unavailable",
                "Choose exact Arr instances, or choose an explicit empty assignment. Each selected connection is configured and checked separately.",
                missing_inputs=[f"{action['source']}::arr_instances"],
            )
        changed = False
        if action["service_key"] in {"prowlarr", "neutarr", "profilarr"}:
            if cfg.get("arr_instances") != selected:
                if read_only:
                    return _result(
                        "waiting_input",
                        "unavailable",
                        "The saved helper assignment differs from this accepted setup choice. Apply the assignment to reconcile only its owned links.",
                    )
                previous = cfg.get("arr_instances")
                cfg["arr_instances"] = list(selected)
                try:
                    self.manager.save_config()
                except Exception:
                    cfg["arr_instances"] = previous
                    raise
                changed = True
        return _result(
            "applied" if changed else "already_present",
            "unavailable",
            "The exact routing assignment is saved. Individual connection tasks report their own configuration and authenticated checks.",
            automation="after_input",
            evidence={"level": "accepted_routing_choice", "targets": list(selected)},
        )

    def _pulsarr_metadata(self, action, payload, *, read_only):
        env = (self.manager.config.get("pulsarr") or {}).get("env") or {}
        value = ((payload.get("arrangement") or {}).get("values") or {}).get(
            f"{action['source']}::tmdb_token"
        )
        if value and value != env.get("tmdbApiKey"):
            if read_only:
                return _result(
                    "waiting_input",
                    "unavailable",
                    "The supplied metadata token has not been saved to Pulsarr's configured environment. Apply this action to save it.",
                )
            section_existed = "pulsarr" in self.manager.config
            section = self.manager.config.setdefault("pulsarr", {})
            env_existed = "env" in section
            env = section.setdefault("env", {})
            key_existed = "tmdbApiKey" in env
            previous = env.get("tmdbApiKey")
            env["tmdbApiKey"] = value
            try:
                self.manager.save_config()
            except Exception:
                if key_existed:
                    env["tmdbApiKey"] = previous
                else:
                    env.pop("tmdbApiKey", None)
                if not env_existed and not env:
                    section.pop("env", None)
                if not section_existed and not section:
                    self.manager.config.pop("pulsarr", None)
                raise
            return _result(
                "applied",
                "unavailable",
                "The metadata token was saved. Restart Pulsarr through its service controls to load the environment change, then check its metadata features. Token authorization has not been tested.",
                evidence={"level": "saved_environment", "restart_required": "pulsarr"},
            )
        return _result(
            "already_present" if env.get("tmdbApiKey") else "waiting_input",
            "unavailable",
            (
                "The metadata token exists in the configured environment. Runtime loading and feature authorization have not been verified."
                if env.get("tmdbApiKey")
                else "Provide your TMDB API Read Access Token for the selected Pulsarr metadata features."
            ),
        )

    def _protection_policy(self, action, payload, result, *, read_only):
        if result.get("verification") != "passed":
            return result
        cfg = resource_config(self.manager.config, action["source"])
        name = cfg.get("process_name")
        values = (payload.get("arrangement") or {}).get("values") or {}
        key = values.get(f"{action['source']}::api_key")
        policy_root = (self.manager.config.get("dumb") or {}).get(
            "media_protection"
        ) or {}
        services = policy_root.get("services") or []
        policy = next(
            (
                item
                for item in services
                if isinstance(item, dict) and item.get("process_name") == name
            ),
            None,
        )
        if policy and policy.get("api_key") == key:
            return {
                **result,
                "reason": "The dedicated protection key was saved and authenticated. Existing enablement, mounts and outage policy were preserved; no outage was simulated.",
            }
        if (
            read_only
            or not key
            or values.get(f"{action['source']}::save_protection_key") is not True
        ):
            return {
                **result,
                "execution": "waiting_input",
                "reason": "Authenticated session/library checks passed. Authorize saving this key for Library Protection, then review enablement and mount policy in the service panel.",
            }
        root = self.manager.config.setdefault("dumb", {}).setdefault(
            "media_protection", {}
        )
        services = root.setdefault("services", [])
        if policy is None:
            policy = {"process_name": name, "enabled": False, "protected_mounts": []}
            services.append(policy)
        previous = policy.get("api_key", "")
        policy["api_key"] = key
        try:
            self.manager.save_config()
        except Exception:
            policy["api_key"] = previous
            raise
        return {
            **result,
            "execution": "applied",
            "reason": "The dedicated protection key was saved and authenticated. Existing outage policy was preserved; a new policy stays disabled until you choose its mounts and enable it.",
        }

    def _runtime(self, action, config):
        from utils.service_health import ServiceHealthMonitor

        cfg = resource_config(config, action["source"])
        key = action["service_key"]
        if key == "authelia" and not cfg.get("public_url"):
            binary = Path(cfg.get("install_dir") or "/authelia") / "authelia"
            if binary.is_file():
                return _result(
                    "applied",
                    "passed",
                    "Authelia is installed and intentionally awaits bootstrap before its first start.",
                    evidence={"level": "installed_waiting_bootstrap"},
                )
        process_name = cfg.get("process_name")
        if not process_name:
            return _result(
                "waiting_dependency",
                "not_checked",
                "Waiting for the selected service instance to be materialized.",
                retryable=True,
            )
        if self.api_state.get_status(process_name) != "running":
            return _result(
                "failed",
                "failed",
                "The selected service is not running. Review its startup result before retrying.",
                retryable=False,
            )
        health = ServiceHealthMonitor(cache_ttl_seconds=0, timeout_seconds=2).check(
            key, process_name, cfg
        )
        if health and health.get("status") != "healthy":
            return _result(
                "waiting_dependency",
                "not_checked",
                "The process is running; its local application readiness check is not yet healthy.",
                retryable=True,
            )
        return _result(
            "already_present",
            "passed",
            "The selected service is running. This check does not verify account setup, integrations or media playback.",
            evidence={"level": "application_ready" if health else "process_running"},
        )

    def _provider_link(self, action, payload, *, read_only):
        from utils.integration_result import integration_targets

        provider = action["action"].partition(".")[2]
        cfg = resource_config(self.manager.config, action["source"])
        if read_only:
            return self._inspect_provider_client(action, cfg, provider)
        outcomes = []
        with integration_targets({action["source"]}):
            if provider == "decypharr":
                from utils.decypharr_settings import patch_decypharr_config

                patch_decypharr_config(result_collector=outcomes)
            elif provider == "infinidysk":
                from utils.nzbdav_settings import patch_nzbdav_config

                patch_nzbdav_config(result_collector=outcomes)
            elif provider == "altmount":
                from utils.altmount_settings import patch_altmount_arr_integration

                patch_altmount_arr_integration(result_collector=outcomes)
        relevant = [
            asdict(item)
            for item in outcomes
            if item.instance_key in {None, action["source"], action.get("instance_key")}
        ]
        self.last_integrations = relevant
        if any(item["execution"] == "failed" for item in relevant):
            return _result(
                "failed",
                "failed",
                "One or more required provider integration settings could not be applied.",
                evidence={"level": "configuration_actions", "actions": relevant},
                retryable=True,
            )
        if any(
            item["execution"] in {"waiting_input", "waiting_dependency"}
            for item in relevant
        ):
            waiting = (
                "waiting_input"
                if any(item["execution"] == "waiting_input" for item in relevant)
                else "waiting_dependency"
            )
            return _result(
                waiting,
                "not_checked",
                "The provider connection needs credentials, application readiness or review of an existing unowned connection.",
                evidence={"level": "configuration_actions", "actions": relevant},
                retryable=waiting == "waiting_dependency",
            )
        result = self._inspect_provider_client(action, cfg, provider)
        result["evidence"] = {**result.get("evidence", {}), "actions": relevant}
        return result

    def _inspect_provider_client(self, action, cfg, provider):
        client = _arr_api(cfg)
        if client is None:
            return _result(
                "waiting_dependency",
                reason="Waiting for the Arr configuration and generated API key.",
                retryable=True,
            )
        version = "v1" if action["service_key"] == "lidarr" else "v3"
        try:
            response = client.get(f"/api/{version}/downloadclient")
            if response.status_code in {401, 403}:
                return _result(
                    "waiting_input",
                    "failed",
                    "The Arr API rejected the configured key.",
                    retryable=False,
                )
            response.raise_for_status()
            clients = response.json()
            if not isinstance(clients, list):
                raise ValueError("Unsupported client response")
            names = {
                "decypharr": {"decypharr", "decypharr-usenet"},
                "infinidysk": {"infinidysk", "nzbdav"},
                "altmount": {"altmount"},
            }
            matches = [
                item
                for item in clients
                if isinstance(item, dict)
                and str(item.get("name", "")).lower() in names.get(provider, {provider})
                and item.get("enable", True)
            ]
            if not matches:
                return _result(
                    "waiting_dependency",
                    reason="The selected enabled provider download client is not configured yet.",
                    retryable=True,
                )
            checks = []
            provider_cfg = self.manager.config.get(provider) or {}
            port = int(
                provider_cfg.get("backend_port", 8080)
                if provider == "infinidysk"
                else provider_cfg.get("port", 8088 if provider == "altmount" else 8282)
            )
            if provider == "decypharr":
                from utils.decypharr_settings import expected_arr_category
            elif provider == "infinidysk":
                from utils.nzbdav_settings import expected_arr_category
            else:
                from utils.altmount_settings import expected_arr_category
            expected_category = expected_arr_category(
                self.manager.config, action["service_key"], action.get("instance_key")
            )
            for item in matches:
                fields = {
                    str(f.get("name", "")).lower(): f.get("value")
                    for f in item.get("fields", [])
                    if isinstance(f, dict)
                }
                categories = [
                    value
                    for key, value in fields.items()
                    if key
                    in {
                        "category",
                        "torrentcategory",
                        "moviecategory",
                        "tvcategory",
                        "musiccategory",
                        "nzbcategory",
                    }
                    and value
                ]
                if (
                    str(fields.get("host", "")).lower()
                    not in {"127.0.0.1", "localhost", "::1"}
                    or int(fields.get("port", 0)) != port
                    or expected_category not in categories
                ):
                    return _result(
                        "waiting_input",
                        "failed",
                        "The existing client endpoint or per-instance category differs from the selected arrangement. Review the existing connection before adopting it.",
                        retryable=False,
                    )
                result = client.post(f"/api/{version}/downloadclient/test", json=item)
                if result.status_code not in {200, 201, 204}:
                    return _result(
                        "waiting_dependency",
                        "failed",
                        "The Arr's selected download-client connection test did not pass.",
                        retryable=True,
                    )
                if result.content:
                    findings = result.json()
                    if isinstance(findings, list) and findings:
                        return _result(
                            "waiting_input",
                            "failed",
                            "The Arr reported download-client validation findings. Review its category, credentials and endpoint.",
                            retryable=False,
                        )
                checks.append(
                    {"client_id": item.get("id"), "connection_test": "passed"}
                )
            evidence = {"level": "connection_test", "clients": checks}
            if provider in {"decypharr", "infinidysk"}:
                if provider == "decypharr":
                    from utils.decypharr_settings import expected_arr_root
                else:
                    from utils.nzbdav_settings import expected_arr_root
                expected = expected_arr_root(
                    self.manager.config,
                    action["service_key"],
                    action.get("instance_key"),
                )
                roots_response = client.get(f"/api/{version}/rootfolder")
                roots_response.raise_for_status()
                roots = roots_response.json()
                if not isinstance(roots, list) or not any(
                    isinstance(root, dict)
                    and os.path.normpath(str(root.get("path", "")))
                    == os.path.normpath(expected)
                    for root in roots
                ):
                    return _result(
                        "waiting_dependency",
                        "failed",
                        "The expected per-instance import root was not read back from the Arr.",
                        retryable=True,
                    )
                permissions_response = client.get(
                    f"/api/{version}/config/mediamanagement"
                )
                permissions_response.raise_for_status()
                permissions = permissions_response.json()
                if (
                    not isinstance(permissions, dict)
                    or not (
                        permissions.get("setPermissions")
                        or permissions.get("setPermissionsLinux")
                    )
                    or str(permissions.get("chmodFolder")) != "777"
                    or (
                        "chmodFile" in permissions
                        and str(permissions["chmodFile"]) != "666"
                    )
                ):
                    return _result(
                        "waiting_dependency",
                        "failed",
                        "The disclosed Arr import permission settings were not read back.",
                        retryable=True,
                    )
                evidence.update(root_path=expected, permission_readback=True)
            return _result(
                "already_present",
                "passed",
                "The expected endpoint, per-instance category and applicable import settings were read back, and the client's authenticated connection test passed. Download, import and playback remain separate checks.",
                evidence=evidence,
            )
        except (ApiError, TypeError, ValueError):
            return _result(
                "waiting_dependency",
                "not_checked",
                "The Arr/provider connection could not yet be checked.",
                retryable=True,
            )

    def preflight(self, plan, payload):
        from utils.arrangement_ports import port_checks
        from utils.arrangement_preflight import (
            filesystem_for,
            mount_inventory,
            storage_checks,
        )
        from utils.arrangement_providers import validate_connection_budgets
        from utils.arrangement_plan import project_configuration

        checks = []
        values = (payload.get("arrangement") or {}).get("values") or {}
        proposed, _ = project_configuration(
            payload, self.manager.config, self.defaults()
        )
        for resource in plan["services"]:
            if resource["service_key"] == "cli_debrid":
                from utils.cli_source_capabilities import (
                    cli_source_capability,
                    effective_battery_mode,
                )

                cfg = resource_config(proposed, resource["id"])
                mode = effective_battery_mode(cfg)
                capability = cli_source_capability(cfg)
                compatible = mode in {"standalone", "embedded"} and (
                    capability["battery_mode"] is None
                    or capability["battery_mode"] == mode
                )
                checks.append(
                    {
                        "key": "cli_battery_layout",
                        "label": "CLI Battery source layout",
                        "ok": compatible,
                        "critical": True,
                        "detail": "Choose the Battery layout supported by the selected CLI source before installing. "
                        + capability["evidence"],
                        "battery_mode": mode,
                        "capability": capability,
                    }
                )
        try:
            budget = validate_connection_budgets(resolve_provider_inputs(payload))
            checks.append(
                {
                    "key": "nntp_budget",
                    "label": "Declared NNTP connection allocations",
                    "ok": True,
                    "critical": False,
                    "detail": "Selected account allocations were checked against declared limits. Unselected applications and unknown subscription limits require operator review.",
                    "summary": budget,
                }
            )
        except ValueError:
            checks.append(
                {
                    "key": "nntp_budget",
                    "label": "NNTP connection budget",
                    "ok": False,
                    "critical": True,
                    "detail": "Selected provider allocations exceed a declared account limit or are invalid.",
                }
            )
        mounts = mount_inventory()
        for resource in plan["services"]:
            cfg = resource_config(proposed, resource["id"])
            if resource["service_key"] == "audiobookshelf":
                path = str(cfg.get("config_dir") or "/audiobookshelf/config")
                kind = filesystem_for(path, mounts)
                checks.append(
                    {
                        "key": "abs_local_config",
                        "label": "Audiobookshelf SQLite storage",
                        "ok": (
                            None
                            if kind == "unknown"
                            else kind
                            not in {"nfs", "nfs4", "cifs", "smb3", "fuse.rclone"}
                        ),
                        "critical": True,
                        "detail": "Audiobookshelf configuration must use local storage; media libraries may use separate mounts.",
                        "filesystem": kind,
                    }
                )
        needs_fuse = False
        for resource in plan["services"]:
            key = resource["service_key"]
            if key == "infinidysk":
                cfg = resource_config(proposed, resource["id"])
                if str(cfg.get("runtime_generation") or "1") == "2":
                    from utils.backup_storage import (
                        BackupStorageError,
                        require_local_path,
                    )
                    from utils.infinidysk_v2_runtime import (
                        resolve_v2_data_dir,
                        runtime_dir,
                    )

                    for field, path in (
                        (
                            "data",
                            resolve_v2_data_dir(
                                cfg, proposed.get("data_root") or "/data"
                            ),
                        ),
                        ("runtime", runtime_dir(cfg)),
                    ):
                        try:
                            require_local_path(path)
                            valid = True
                        except (BackupStorageError, OSError, ValueError):
                            valid = False
                        checks.append(
                            {
                                "key": "native_local_" + field,
                                "label": "Native InfiniDysk " + field + " storage",
                                "ok": valid,
                                "critical": True,
                                "detail": "Active native data, runtime and SQLite/control state require verified local storage; registered network destinations hold closed backups only.",
                            }
                        )
                    if cfg.get("mount_backend") == "fuse":
                        needs_fuse = True
            if key in {"rclone", "zurg"}:
                needs_fuse = True
            if key in {"decypharr", "altmount"}:
                cfg = resource_config(proposed, resource["id"])
                default_mode = "dfs" if key == "decypharr" else "rclone"
                mode = str(cfg.get("mount_type") or default_mode).strip().lower()
                if mode in {"dfs", "rclone"}:
                    needs_fuse = True
        native_fuse = any(
            item["service_key"] == "infinidysk"
            and str(
                resource_config(proposed, item["id"]).get("runtime_generation") or "1"
            )
            == "2"
            and resource_config(proposed, item["id"]).get("mount_backend") == "fuse"
            for item in plan["services"]
        )
        if native_fuse:
            import shutil

            try:
                uid = int(
                    proposed.get("puid") if proposed.get("puid") is not None else 1000
                )
                allowed = uid == 0 or any(
                    line.split("#", 1)[0].strip() == "user_allow_other"
                    for line in Path("/etc/fuse.conf").read_text().splitlines()
                )
            except (OSError, ValueError, TypeError):
                allowed = False
            checks.append(
                {
                    "key": "native_fuse_policy",
                    "label": "Native FUSE mount support",
                    "ok": bool(shutil.which("fusermount3")) and allowed,
                    "critical": True,
                    "detail": "Native FUSE requires fusermount3 and user_allow_other for an unprivileged service owner; actual mount and consumer readiness are checked by the native workflow.",
                }
            )
        if needs_fuse:
            checks.append(
                {
                    "key": "fuse_device",
                    "label": "Container FUSE device",
                    "ok": Path("/dev/fuse").exists(),
                    "critical": True,
                    "detail": "Mount workflows require host-provided /dev/fuse and the deployment's mount permissions. DUMB cannot add Docker devices to its running container.",
                }
            )
        checks.extend(
            storage_checks(
                plan, payload, self.manager.config, self.defaults(), mounts=mounts
            )
        )
        checks.extend(
            port_checks(
                plan,
                payload,
                self.manager.config,
                self.defaults(),
                status=self.api_state.get_status,
            )
        )
        checks.append(
            {
                "key": "storage_scope",
                "label": "Host deployment and content availability",
                "ok": None,
                "critical": False,
                "detail": "Local metadata checks do not establish remote content availability or host bind/device mappings. Verify each consumer namespace; deliberate download/import/playback tests require a separate user action.",
            }
        )
        return checks
