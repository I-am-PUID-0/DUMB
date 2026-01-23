def _normalize_service_name(value):
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def normalize_core_services(value):
    services = []
    if value is None:
        return services
    if isinstance(value, str):
        parts = value.split(",") if "," in value else [value]
        for part in parts:
            name = _normalize_service_name(part)
            if name:
                services.append(name)
        return services
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, str):
                parts = item.split(",") if "," in item else [item]
                for part in parts:
                    name = _normalize_service_name(part)
                    if name:
                        services.append(name)
    return services


def get_core_services(inst_cfg):
    if not isinstance(inst_cfg, dict):
        return []
    merged = []
    for raw in (inst_cfg.get("core_services"), inst_cfg.get("core_service")):
        merged.extend(normalize_core_services(raw))
    seen = set()
    result = []
    for name in merged:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def has_core_service(inst_cfg, service_name):
    name = _normalize_service_name(service_name)
    if not name:
        return False
    return name in get_core_services(inst_cfg)
