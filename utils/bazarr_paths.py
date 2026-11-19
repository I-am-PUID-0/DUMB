"""Keep Bazarr's writable runtime data separate from its application tree."""


def bazarr_data_directory(config):
    command = config.get("command") or []
    if isinstance(command, list):
        try:
            configured = str(command[command.index("--config") + 1]).strip()
            if configured:
                return configured
        except (ValueError, IndexError):
            pass
    return "/bazarr/data"
