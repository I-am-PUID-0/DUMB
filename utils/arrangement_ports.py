"""Local listener suitability at preview time; no host mappings are inferred."""

from __future__ import annotations

import socket

from utils.arrangement_plan import project_configuration, resource_config

PORT_FIELDS = ("port", "backend_port", "frontend_port", "webdav_port", "rclone_rc_port")


def _available(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # Probe the wildcard listener's eventual bind semantics, then close.
            # No listen/accept or traffic handling occurs on this socket.
            # codeql[py/bind-socket-all-network-interfaces]
            probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False


def port_checks(
    plan, payload, current, defaults, *, status=lambda _: None, available=_available
):
    proposed, _ = project_configuration(payload, current, defaults)
    seen, checks = {}, []
    for resource in plan["services"]:
        cfg = resource_config(proposed, resource["id"])
        for field in PORT_FIELDS:
            port = cfg.get(field)
            if port is None:
                continue
            if type(port) is not int or not 1 <= port <= 65535:
                checks.append(
                    {
                        "key": f"port:{resource['id']}:{field}",
                        "label": "Application listener port",
                        "ok": False,
                        "critical": True,
                        "detail": "Choose a valid TCP port before installing.",
                        "resource_id": resource["id"],
                    }
                )
                continue
            previous = seen.setdefault(port, resource["id"] + ":" + field)
            same = previous == resource["id"] + ":" + field
            owned = (
                status(cfg.get("process_name", "")) == "running"
                and resource_config(current, resource["id"]).get(field) == port
            )
            free = available(port) if same and not owned else None
            detail = "The TCP port is available inside this container at preview time. It is not reserved; Docker host exposure is a separate deployment setting."
            if not same:
                detail = "Selected listeners share this port. Startup may choose another free port; review the resulting service port and any Docker host mapping afterward."
            elif owned:
                detail = "The configured port belongs to this running managed resource. Its application identity and authentication are checked separately."
            elif free is False:
                detail = "The configured port is occupied inside the container. Startup may assign another port; review the effective port and host mapping after setup."
            checks.append(
                {
                    "key": f"port:{resource['id']}:{field}",
                    "label": f"{resource['name']} {field}: {port}",
                    "ok": True if same and free is True else None,
                    "critical": False,
                    "resource_id": resource["id"],
                    "port": port,
                    "detail": detail,
                }
            )
    return checks
