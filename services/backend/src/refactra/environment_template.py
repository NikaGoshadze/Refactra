"""Prepare and apply additive example configuration changes."""

import re

MAX_TEMPLATE_BYTES = 64 * 1024
_NAME = rb"[A-Za-z_][A-Za-z0-9_]*"
_EXISTING = re.compile(rb"^\s*(?:export\s+)?(" + _NAME + rb")\s*=")
_ENTRY = re.compile(rb"(" + _NAME + rb")=(.*)")
_SCALAR = re.compile(rb"(?:-?\d{1,9}(?:\.\d{1,6})?|true|false)", re.IGNORECASE)
_CREDENTIAL = re.compile(
    rb"(?:^|_)(?:APIKEY|KEYS?|TOKENS?|PASSWORDS?|PASS|PASSWD|SECRET|SECRETS|"
    rb"CREDENTIALS?|AUTH|PIN|PRIVATE)(?:_|$)",
    re.IGNORECASE,
)


def _names(source: bytes) -> dict[bytes, None]:
    return dict.fromkeys(
        match[1] for line in source.splitlines() if (match := _EXISTING.match(line))
    )


def template_preview(source: bytes) -> bytes:
    """Expose assignment names while keeping existing values and comments private."""
    if len(source) > MAX_TEMPLATE_BYTES or b"\0" in source:
        raise ValueError("Environment templates must be bounded UTF-8 text.")
    try:
        source.decode("utf-8")
    except UnicodeError:
        raise ValueError("Environment templates must be bounded UTF-8 text.") from None
    return b"".join(name + b"=\n" for name in _names(source))


def append_template_entries(source: bytes, additions: bytes) -> bytes:
    names = set(_names(source))
    entries = []
    for line in additions.splitlines():
        if not line.strip():
            continue
        if not (match := _ENTRY.fullmatch(line.strip())):
            raise ValueError("Template updates must contain one new assignment per line.")
        name, value = match.groups()
        if name in names:
            raise ValueError("Template updates must preserve existing assignments.")
        if value and (_CREDENTIAL.search(name) or not _SCALAR.fullmatch(value)):
            raise ValueError(
                "Use empty values or numeric and boolean defaults for non-credential settings."
            )
        names.add(name)
        entries.append(name + b"=" + value)
    if not entries:
        return source
    newline = b"\r\n" if b"\r\n" in source else b"\n"
    separator = newline if source and not source.endswith(b"\n") else b""
    updated = source + separator + newline.join(entries) + newline
    if len(updated) > MAX_TEMPLATE_BYTES:
        raise ValueError("The updated environment template exceeds the size limit.")
    return updated
