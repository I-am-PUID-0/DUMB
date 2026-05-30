import urllib.request
from urllib.parse import urlparse

ALLOWED_URL_SCHEMES = {"http", "https"}


def validate_url_scheme(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_URL_SCHEMES:
        allowed = ", ".join(sorted(ALLOWED_URL_SCHEMES))
        raise ValueError(f"Unsupported URL scheme for {url!r}; expected {allowed}")
    if not parsed.netloc:
        raise ValueError(f"Missing host for URL: {url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"User credentials are not allowed in URL: {url!r}")
    return url


def safe_request(url: str, **kwargs) -> urllib.request.Request:
    safe_url = validate_url_scheme(url)
    return urllib.request.Request(safe_url, **kwargs)  # noqa: S310


def safe_urlopen(target, **kwargs):
    url = target.full_url if isinstance(target, urllib.request.Request) else target
    validate_url_scheme(url)
    # The URL scheme is validated immediately above and Request construction goes
    # through safe_request at the call sites covered by Ruff S310.
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310
