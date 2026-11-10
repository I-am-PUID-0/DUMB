"""Audiobookshelf service paths and controller-managed launch environment."""

from copy import deepcopy
import os
import shutil


class AudiobookshelfConfigurationError(ValueError):
    """The configured Audiobookshelf service cannot be launched safely."""


def audiobookshelf_paths(config):
    defaults = {
        "runtime_dir": "/opt/audiobookshelf",
        "config_dir": "/audiobookshelf/config",
        "metadata_dir": "/audiobookshelf/metadata",
    }
    paths = {}
    for field, fallback in defaults.items():
        value = config.get(field, fallback)
        if (
            not isinstance(value, str)
            or not os.path.isabs(value)
            or "\x00" in value
            or os.path.realpath(value) == "/"
        ):
            raise AudiobookshelfConfigurationError(
                f"Audiobookshelf {field} must be an absolute directory below the filesystem root."
            )
        paths[field] = os.path.normpath(value)
    runtime = os.path.realpath(paths["runtime_dir"])
    for field in ("config_dir", "metadata_dir"):
        data = os.path.realpath(paths[field])
        if os.path.commonpath((runtime, data)) in {runtime, data}:
            raise AudiobookshelfConfigurationError(
                "Audiobookshelf runtime must be separate from its persistent config and metadata directories."
            )
    if os.path.realpath(paths["config_dir"]) == os.path.realpath(paths["metadata_dir"]):
        raise AudiobookshelfConfigurationError(
            "Audiobookshelf config and metadata directories must be separate."
        )
    return paths


def audiobookshelf_launch_config(config):
    paths = audiobookshelf_paths(config)
    try:
        port = config.get("port", 13378)
        if isinstance(port, bool):
            raise ValueError
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise AudiobookshelfConfigurationError(
            "Audiobookshelf port must be between 1 and 65535."
        ) from error

    binaries = {}
    for name in ("node", "ffmpeg", "ffprobe"):
        binary = shutil.which(name)
        if not binary:
            raise AudiobookshelfConfigurationError(
                f"Audiobookshelf requires {name}; update or rebuild the DUMB image."
            )
        binaries[name] = binary
    env = deepcopy(config.get("env") or {})
    env.update(
        {
            "NODE_ENV": "production",
            "HOST": "0.0.0.0",
            "PORT": str(port),
            "CONFIG_PATH": paths["config_dir"],
            "METADATA_PATH": paths["metadata_dir"],
            "ROUTER_BASE_PATH": "/audiobookshelf",
            "ALLOW_IFRAME": "1",
            # Use the image's maintained FFmpeg binaries and standard SQLite.
            # Upstream's startup downloader must not alter the managed runtime.
            "SKIP_BINARIES_CHECK": "1",
            "FFMPEG_PATH": binaries["ffmpeg"],
            "FFPROBE_PATH": binaries["ffprobe"],
        }
    )
    return {
        **paths,
        "command": [
            binaries["node"],
            os.path.join(paths["runtime_dir"], "dist-server", "index.js"),
        ],
        "env": env,
    }
