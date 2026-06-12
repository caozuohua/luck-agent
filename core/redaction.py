from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


REDACTED = "[REDACTED]"
REDACTION_FAILED = "[REDACTION_FAILED]"
MAX_DEPTH = "[MAX_DEPTH]"
MAX_NODES = "[MAX_NODES]"
CIRCULAR = "[CIRCULAR]"

_SENSITIVE_KEYS = frozenset({
    "accesskey",
    "accesstoken",
    "apikey",
    "appsecret",
    "authorization",
    "clientsecret",
    "cookie",
    "key",
    "proxyauthorization",
    "refreshtoken",
    "secret",
    "setcookie",
    "signature",
    "ticket",
    "token",
})
_KEY_PATTERN = (
    r"(?:access[_-]?key|access[_-]?token|api[_-]?key|app[_-]?secret|"
    r"authorization|client[_-]?secret|cookie|key|proxy[_-]?authorization|"
    r"refresh[_-]?token|secret|set[_-]?cookie|signature|ticket|token)"
)
_HEADER_PATTERN = re.compile(
    rf"(?im)(?P<prefix>\b(?:authorization|proxy-authorization|cookie|"
    rf"set-cookie)\s*:\s*)(?P<value>[^\r\n]+)"
)
_QUOTED_FIELD_PATTERN = re.compile(
    rf"(?is)(?P<prefix>[\"']?{_KEY_PATTERN}[\"']?\s*[:=]\s*)"
    rf"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_UNQUOTED_FIELD_PATTERN = re.compile(
    rf"(?i)(?P<prefix>(?:^|[?&\s,{{]){_KEY_PATTERN}\s*[:=]\s*)"
    rf"(?!\[REDACTED\])(?P<value>[^\s,}}\]&]+)"
)
_AUTH_VALUE_PATTERN = re.compile(
    r"(?i)\b(?P<scheme>bearer|basic)\s+(?P<value>[A-Za-z0-9._~+/=-]+)"
)

_configured_secrets: tuple[str, ...] = ()


def configure_redaction_secrets(secrets: Iterable[str]) -> None:
    """Replace process-wide literal secrets used by observable-output guards."""
    global _configured_secrets
    normalized = {
        str(secret)
        for secret in secrets
        if secret is not None and str(secret)
    }
    _configured_secrets = tuple(sorted(normalized, key=len, reverse=True))


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _is_sensitive_key(value: object) -> bool:
    return _normalized_key(value) in _SENSITIVE_KEYS


def _all_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    values = set(_configured_secrets)
    values.update(
        str(secret)
        for secret in secrets
        if secret is not None and str(secret)
    )
    return tuple(sorted(values, key=len, reverse=True))


def redact_text(
    value: object,
    *,
    secrets: Iterable[str] = (),
) -> str:
    """Redact credential-shaped text without ever returning unsafe failures."""
    try:
        text = str(value)
        text = _HEADER_PATTERN.sub(
            lambda match: f"{match.group('prefix')}{REDACTED}",
            text,
        )
        text = _QUOTED_FIELD_PATTERN.sub(
            lambda match: (
                f"{match.group('prefix')}{match.group('quote')}"
                f"{REDACTED}{match.group('quote')}"
            ),
            text,
        )
        text = _UNQUOTED_FIELD_PATTERN.sub(
            lambda match: f"{match.group('prefix')}{REDACTED}",
            text,
        )
        text = _AUTH_VALUE_PATTERN.sub(
            lambda match: f"{match.group('scheme')} {REDACTED}",
            text,
        )
        for secret in _all_secrets(secrets):
            text = text.replace(secret, REDACTED)
        return text
    except Exception:
        return REDACTION_FAILED


def redact_value(
    value: object,
    *,
    secrets: Iterable[str] = (),
    max_depth: int = 8,
    max_nodes: int = 500,
) -> Any:
    """Recursively redact structured values with deterministic safety bounds."""
    try:
        secret_values = _all_secrets(secrets)
        nodes = [0]
        active_ids: set[int] = set()

        def clean(item: object, depth: int) -> Any:
            if depth > max(0, int(max_depth)):
                return MAX_DEPTH
            nodes[0] += 1
            if nodes[0] > max(1, int(max_nodes)):
                return MAX_NODES

            if item is None or isinstance(item, (bool, int, float)):
                return item
            if isinstance(item, str):
                return redact_text(item, secrets=secret_values)
            if isinstance(item, Mapping):
                identity = id(item)
                if identity in active_ids:
                    return CIRCULAR
                active_ids.add(identity)
                try:
                    result: dict[Any, Any] = {}
                    for key, nested in item.items():
                        safe_key = (
                            redact_text(key, secrets=secret_values)
                            if isinstance(key, str)
                            else key
                        )
                        if _is_sensitive_key(key):
                            result[safe_key] = REDACTED
                        else:
                            result[safe_key] = clean(nested, depth + 1)
                        if nodes[0] > max_nodes:
                            break
                    return result
                finally:
                    active_ids.remove(identity)
            if isinstance(item, (list, tuple)):
                identity = id(item)
                if identity in active_ids:
                    return CIRCULAR
                active_ids.add(identity)
                try:
                    cleaned: list[Any] = []
                    for nested in item:
                        cleaned.append(clean(nested, depth + 1))
                        if nodes[0] > max_nodes:
                            break
                    return tuple(cleaned) if isinstance(item, tuple) else cleaned
                finally:
                    active_ids.remove(identity)
            return redact_text(item, secrets=secret_values)

        return clean(value, 0)
    except Exception:
        return REDACTION_FAILED
