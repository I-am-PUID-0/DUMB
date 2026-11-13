"""Pure ordering of an exact retained process set; never probes or starts services."""

from copy import deepcopy
import ipaddress
import posixpath
from urllib.parse import urlsplit


class BackupLifecycleError(RuntimeError):
    """The retained process set cannot be resumed unambiguously."""


def _services(config):
    for key, section in config.items():
        if not isinstance(section, dict) or key == "dumb":
            continue
        instances = section.get("instances")
        rows = instances.values() if isinstance(instances, dict) else [section]
        for service in rows:
            if isinstance(service, dict) and isinstance(
                service.get("process_name"), str
            ):
                yield key, service


def _path(value):
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or "\x00" in value
        or ".." in value.split("/")
    ):
        return None
    return posixpath.normpath(value)


def _local_host(value):
    value = str(value or "127.0.0.1").lower().rstrip(".")
    if value in {"localhost", "0.0.0.0", "::"}:
        return True
    try:
        address = ipaddress.ip_address(value)
        return address.is_loopback or bool(
            getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback
        )
    except ValueError:
        return False


def _port(value):
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if 0 < value < 65536 else None


def _mounts(key, service):
    paths = []
    if key == "rclone":
        parent, name = service.get("mount_dir"), service.get("mount_name")
        if isinstance(parent, str) and isinstance(name, str):
            paths.append(posixpath.join(parent, name))
    elif key == "infinidysk":
        if (
            str(service.get("runtime_generation", "1")) == "2"
            and service.get("mount_backend") == "fuse"
        ):
            paths.append(service.get("mount_path") or "/mnt/debrid/infinidysk")
    elif key == "decypharr":
        if service.get("mount_type", "dfs") == "dfs":
            paths.append(service.get("mount_path"))
    elif key == "altmount":
        paths.extend(service.get(field) for field in ("mount_path", "mount_dir"))
    return {path for value in paths if (path := _path(value)) is not None}


def _directories(key, service):
    broad = {"/", "/data", "/config", "/mnt", "/mnt/debrid", "/usr", "/opt"}
    paths = _mounts(key, service)
    for field in (
        "config_dir",
        "install_dir",
        "runtime_dir",
        "data_dir",
        "metadata_dir",
        "v2_data_dir",
        "v2_runtime_dir",
    ):
        if (path := _path(service.get(field))) is not None and path not in broad:
            paths.add(path)
    return paths


def _selected(recorded, config):
    if not isinstance(recorded, (list, tuple)):
        raise BackupLifecycleError("The retained process set is malformed.")
    configured = {}
    for key, service in _services(config):
        configured.setdefault(service["process_name"], []).append((key, service))
    selected = {}
    for row in recorded:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not row["name"]
            or row.get("key") in {"dumb", "dumb_frontend"}
            or row["name"] in selected
        ):
            raise BackupLifecycleError(
                "The retained process set is malformed or ambiguous."
            )
        matches = configured.get(row["name"], [])
        if len(matches) != 1 or matches[0][0] != row.get("key"):
            raise BackupLifecycleError(
                "A retained process no longer has one matching configured service."
            )
        selected[row["name"]] = (row, *matches[0])
    return selected


def _path_provider(consumer, path, providers):
    target = _path(path)
    if target is None:
        return None
    matches = [
        (len(root), name)
        for name, roots in providers.items()
        if name != consumer
        for root in roots
        if target == root or target.startswith(root.rstrip("/") + "/")
    ]
    if not matches:
        return None
    longest = max(size for size, _name in matches)
    names = {name for size, name in matches if size == longest}
    if len(names) != 1:
        raise BackupLifecycleError(
            "A configured startup wait has multiple retained path providers."
        )
    return names.pop()


def resume_wait_dependencies(recorded, config):
    """Map configured waits only to providers already in the exact retained set."""
    selected = _selected(recorded, config)
    mounts = {
        name: _mounts(key, service) for name, (_row, key, service) in selected.items()
    }
    directories = {
        name: _directories(key, service)
        for name, (_row, key, service) in selected.items()
    }
    endpoints = {}
    for name, (_row, _key, service) in selected.items():
        port = _port(service.get("port"))
        if port is not None and _local_host(service.get("host")):
            endpoints.setdefault(port, set()).add(name)
    dependencies = {name: set() for name in selected}
    for name, (_row, _key, service) in selected.items():
        waits = service.get("wait_for_mounts") or []
        if isinstance(waits, str):
            waits = [waits]
        if isinstance(waits, (list, tuple)):
            for path in waits:
                if provider := _path_provider(name, path, mounts):
                    dependencies[name].add(provider)
        if provider := _path_provider(name, service.get("wait_for_dir"), directories):
            dependencies[name].add(provider)
        ports = []
        tcp = service.get("wait_for_tcp") or []
        if isinstance(tcp, dict):
            tcp = [tcp]
        if isinstance(tcp, (list, tuple)):
            ports.extend(
                _port(item.get("port"))
                for item in tcp
                if isinstance(item, dict) and _local_host(item.get("host"))
            )
        urls = service.get("wait_for_url") or []
        if isinstance(urls, (list, tuple)):
            for item in urls:
                if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                    continue
                try:
                    parsed = urlsplit(item["url"])
                    if (
                        parsed.scheme in {"http", "https"}
                        and parsed.hostname
                        and _local_host(parsed.hostname)
                    ):
                        ports.append(
                            parsed.port or (443 if parsed.scheme == "https" else 80)
                        )
                except ValueError:
                    continue
        for port in ports:
            providers = endpoints.get(port, set()) - {name}
            if len(providers) > 1:
                raise BackupLifecycleError(
                    "A configured startup wait has multiple retained endpoint providers."
                )
            dependencies[name].update(providers)
    return dependencies


def order_resume_rows(recorded, config, key_dependencies):
    """Preserve existing type constraints and add exact configured wait edges.

    key_dependencies is the controller's shared conditional family map. A
    headless recovery dry run supplies that reviewed map explicitly, so this
    module never imports a live configuration singleton or touches the filesystem.
    """
    selected = _selected(recorded, config)
    dependencies = resume_wait_dependencies(recorded, config)
    for name, (_row, key, _service) in selected.items():
        required = key_dependencies.get(key, set())
        dependencies[name].update(
            provider
            for provider, (_other, provider_key, _value) in selected.items()
            if provider != name and provider_key in required
        )
    remaining, ordered = set(selected), []
    while remaining:
        ready = next(
            (
                name
                for name in selected
                if name in remaining and not (dependencies[name] & remaining)
            ),
            None,
        )
        if ready is None:
            raise BackupLifecycleError(
                "The retained startup dependencies contain a cycle; no services were started."
            )
        ordered.append(deepcopy(selected[ready][0]))
        remaining.remove(ready)
    return ordered
