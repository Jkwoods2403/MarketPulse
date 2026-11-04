"""Redaction of credentials from URLs, headers, and free text.

Defense in depth: secrets are removed both by *name* (query parameters and headers known to
carry credentials) and by *value* (any known secret string, raw or URL-encoded). Redacted
output is safe to log, store in the database, and show in error messages.
"""

import re
from collections.abc import Iterable, Mapping
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

REDACTED = "REDACTED"

# Query parameter names that carry credentials (compared case-insensitively).
SENSITIVE_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "api-key",
        "apikey",
        "client_secret",
        "key",
        "password",
        "secret",
        "subscription-key",
        "subscription_key",
        "token",
    }
)

# Header names that carry credentials (compared case-insensitively).
SENSITIVE_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "ocp-apim-subscription-key",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
    }
)

# A credential parameter name followed by "=", anywhere in text (URLs, exception messages, log
# lines), as long as the name is not the tail of a longer word ("monkey=" is not a match).
_SECRET_PARAM_IN_TEXT = re.compile(
    r"(?i)((?<![A-Za-z0-9_-])(?:"
    + "|".join(re.escape(name) for name in sorted(SENSITIVE_PARAM_NAMES, key=len, reverse=True))
    + r")=)[^&;#\s\"'<>,]*"
)

# Secrets shorter than this are not substring-replaced, to avoid mangling unrelated text.
_MIN_SECRET_LENGTH = 4


def _sensitive_names(extra: Iterable[str]) -> frozenset[str]:
    return SENSITIVE_PARAM_NAMES | {name.lower() for name in extra}


def redact_url(url: str, extra_param_names: Iterable[str] = ()) -> str:
    """Return `url` with credential query parameters and any userinfo password replaced."""
    names = _sensitive_names(extra_param_names)
    parts = urlsplit(url)

    netloc = parts.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:{REDACTED}@{host}" if ":" in userinfo else f"{REDACTED}@{host}"

    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = urlencode(
            [(k, REDACTED if k.lower() in names else v) for k, v in pairs],
            safe="[]:,/",
        )

    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_headers(
    headers: Mapping[str, str], extra_header_names: Iterable[str] = ()
) -> dict[str, str]:
    """Return a copy of `headers` with credential headers replaced."""
    names = SENSITIVE_HEADER_NAMES | {name.lower() for name in extra_header_names}
    return {k: (REDACTED if k.lower() in names else v) for k, v in headers.items()}


def exposed_credential_parameters(text: str) -> list[str]:
    """Names of credential parameters in `text` that carry an actual value (for auditing).

    A parameter counts only when it has a non-empty value other than REDACTED: an empty value
    (`token=`) or an already-redacted one exposes nothing. Values are never returned.
    """
    names = []
    for match in _SECRET_PARAM_IN_TEXT.finditer(text):
        prefix = match.group(1)  # "<name>="
        value = match.group(0)[len(prefix) :]
        if value and value != REDACTED:
            names.append(prefix[:-1].lower())
    return names


def contains_secret(text: str, secrets: Iterable[str]) -> bool:
    """Whether any known secret (raw or URL-encoded) appears in `text` (for auditing)."""
    return any(
        form in text
        for secret in secrets
        if len(secret) >= _MIN_SECRET_LENGTH
        for form in {secret, quote(secret, safe=""), quote_plus(secret)}
    )


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove known secret values (raw and URL-encoded) and credential query params from text."""
    for secret in secrets:
        if len(secret) < _MIN_SECRET_LENGTH:
            continue
        for form in {secret, quote(secret, safe=""), quote_plus(secret)}:
            text = text.replace(form, REDACTED)
    return _SECRET_PARAM_IN_TEXT.sub(rf"\g<1>{REDACTED}", text)
