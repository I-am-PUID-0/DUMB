"""Present the fields needed by one action without duplicating every form."""

AUTH = {
    "api_key",
    "username",
    "password",
    "admin_username",
    "admin_password",
    "settings_session",
    "settings_token",
}
ADOPTION = {"adopt_existing", "adoption_preview_id"}
PROBES = {"workflow_probe", "workflow_preview_id", "workflow_cleanup", "consumer_paths"}


def action_fields(action, fields):
    source, name = action["source"], action["action"]
    scoped = [field for field in fields if field.get("resource_id") == source]
    selected = []
    for field in scoped:
        suffix = field["key"].removeprefix(source + "::")
        if name == "workflow_test":
            keep = suffix in {
                "workflow_probe",
                "workflow_preview_id",
                "workflow_cleanup",
            }
        elif name == "consumer_paths":
            keep = suffix == "consumer_paths"
        elif (
            action.get("service_key") == "riven_backend"
            and name == "backend_connection"
        ):
            keep = suffix == "api_key"
        elif (
            action.get("service_key") == "riven_backend" and name == "media_connection"
        ):
            keep = suffix in AUTH | ADOPTION | {"media_server", "external_media"}
        elif action.get("service_key") == "traefik_proxy_admin" and name == "account":
            keep = suffix in AUTH
        elif (
            action.get("service_key") == "traefik_proxy_admin"
            and name == "proxy_routes"
        ):
            keep = suffix in AUTH | ADOPTION | {
                "route_domains",
                "route_services",
                "authorize_public_exposure",
            }
        elif suffix in PROBES:
            keep = False
        elif name == "external_ready":
            keep = False
        elif name == "nntp":
            keep = suffix in AUTH | {
                "nntp",
                "nntp_source",
                "nntp_connections",
                "allow_private_nntp",
            }
        elif name == "import_layout":
            keep = suffix in AUTH | {"import_strategy", "import_dir"}
        elif name.startswith("provider."):
            provider = name.partition(".")[2]
            keep = suffix in AUTH | ADOPTION | {
                "root_folder",
                "new_root_folder",
                "quality_profile",
                "metadata_profile",
            } or suffix.startswith(provider + "::")
        elif name == "arr_connections":
            keep = suffix in AUTH | {"arr_instances"}
        elif name == "arr_connection":
            keep = suffix in AUTH | ADOPTION | {
                "sync_level",
                "enable_integrations",
            } or suffix.startswith(str(action.get("target")) + "::")
            if action.get("service_key") == "bazarr" and suffix in {
                "language_profile",
                "languages",
            }:
                keep = True
        elif name == "indexers":
            keep = suffix in AUTH | ADOPTION | {"indexers"} or suffix.startswith(
                "indexer::"
            )
        elif name == "arr_preferences":
            keep = suffix in AUTH | ADOPTION | {
                "root_folder",
                "quality_profile",
                "metadata_profile",
            }
        elif name == "subtitle_providers":
            keep = suffix in AUTH | ADOPTION | {"providers"} or suffix.startswith(
                "provider::"
            )
        elif action.get("service_key") == "altmount" and name == "account":
            keep = suffix in AUTH | {"create_admin"}
        elif action.get("service_key") == "altmount" and name == "webdav":
            keep = suffix in AUTH | ADOPTION | {
                "webdav_username",
                "webdav_password",
                "restart_after_config",
                "acknowledge_external_consumers",
            }
        elif name == "metadata" and action.get("service_key") == "pulsarr":
            keep = suffix == "tmdb_token"
        elif name == "runtime":
            keep = False
        else:
            keep = True
        if keep:
            selected.append(field["key"])
    return selected


def bind_action_fields(record):
    fields = record.get("arrangement_schema", {}).get("fields", [])
    for action in record.get("actions", []):
        action["input_fields"] = action_fields(action, fields)
