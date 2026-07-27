import os
import stat

INITIAL_ADMIN_PASSWORD_FILENAMES = (
    "initial_admin_password.txt",
    "initial_admin_password",
)
MAX_INITIAL_ADMIN_PASSWORD_BYTES = 4096


class MediaStormCredentialError(ValueError):
    """Raised when mediastorm's bootstrap credential file is unsafe or invalid."""


def read_initial_admin_password(config_dir: str) -> str | None:
    """Read mediastorm's one-time initial admin password without following symlinks."""

    cache_dir = os.path.join(config_dir, "cache")
    for filename in INITIAL_ADMIN_PASSWORD_FILENAMES:
        path = os.path.join(cache_dir, filename)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)

        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise MediaStormCredentialError(
                "mediastorm's initial credential file could not be opened safely."
            ) from exc

        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise MediaStormCredentialError(
                    "mediastorm's initial credential path is not a regular file."
                )
            if file_stat.st_size > MAX_INITIAL_ADMIN_PASSWORD_BYTES:
                raise MediaStormCredentialError(
                    "mediastorm's initial credential file is unexpectedly large."
                )

            value = os.read(descriptor, MAX_INITIAL_ADMIN_PASSWORD_BYTES + 1)
        finally:
            os.close(descriptor)

        if len(value) > MAX_INITIAL_ADMIN_PASSWORD_BYTES:
            raise MediaStormCredentialError(
                "mediastorm's initial credential file is unexpectedly large."
            )

        try:
            password = value.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError as exc:
            raise MediaStormCredentialError(
                "mediastorm's initial credential file is not valid UTF-8."
            ) from exc

        if not password or "\x00" in password or "\r" in password or "\n" in password:
            raise MediaStormCredentialError(
                "mediastorm's initial credential file has an invalid value."
            )

        return password

    return None
