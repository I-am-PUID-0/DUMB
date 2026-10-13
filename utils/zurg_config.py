"""Read and update Zurg's root settings without rewriting provider settings."""

from collections.abc import MutableMapping
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from utils.private_files import atomic_write_private_text


class ZurgConfigError(ValueError):
    """The configuration cannot be safely read or updated."""


def _load_document(content):
    parser = YAML(typ="rt")
    parser.preserve_quotes = True
    parser.allow_duplicate_keys = False
    try:
        document = parser.load(content)
    except YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        # YAML exceptions include source snippets, which can contain credentials.
        raise ZurgConfigError(f"Invalid Zurg YAML{location}") from None
    if not isinstance(document, MutableMapping):
        raise ZurgConfigError("Zurg configuration must contain one root mapping")
    return parser, document


def _port_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ZurgConfigError("Zurg port must be an integer between 1 and 65535")
    try:
        port = int(value)
    except ValueError:
        raise ZurgConfigError(
            "Zurg port must be an integer between 1 and 65535"
        ) from None
    if not 1 <= port <= 65535:
        raise ZurgConfigError("Zurg port must be an integer between 1 and 65535")
    return port


def update_zurg_config(config_file_path, updates=None, *, remove=()):
    """Validate root-only edits before atomically replacing the private file."""
    # Existing configuration links must keep pointing at the managed file.
    path = Path(config_file_path).resolve(strict=True)
    parser, document = _load_document(path.read_text(encoding="utf-8"))
    updates = updates or {}
    for key, value in updates.items():
        document[key] = (
            _port_number(value)
            if key == "port"
            else DoubleQuotedScalarString(str(value))
        )
    for key in remove:
        document.pop(key, None)

    output = StringIO()
    try:
        parser.dump(document, output)
    except YAMLError:
        raise ZurgConfigError("Unable to render Zurg configuration") from None
    rendered = output.getvalue()
    _, validated = _load_document(rendered)
    if any(key in validated for key in remove):
        raise ZurgConfigError(
            "Cannot remove root credentials inherited from a YAML merge"
        )
    atomic_write_private_text(path, rendered)


def read_zurg_port(config_file_path):
    """Return the root listener port as text, never an NNTP provider's port."""
    _, document = _load_document(Path(config_file_path).read_text(encoding="utf-8"))
    value = document.get("port")
    if value is None or value == "":
        return "9999"
    return str(_port_number(value))
