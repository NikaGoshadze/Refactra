"""Identify explicit file requirements without interpreting free-form intent as authority."""

import re

_ACTIONS = {
    "create": "CREATE",
    "add": "CREATE",
    "update": "MODIFY",
    "edit": "MODIFY",
    "modify": "MODIFY",
    "delete": "DELETE",
    "remove": "DELETE",
}
_FILE_REQUEST = re.compile(
    r"(?:^|[.!?]\s+|\n|\band\s+)(?P<negated>do not |don't |skip )?"
    r"(?P<verb>create|add|update|edit|modify|delete|remove)\s+(?:the file |file )?"
    r"(?P<quote>`?)(?P<path>[\w./-]+?)(?P=quote)(?=[\s,;.!?]*(?:\s|$))",
    re.IGNORECASE,
)
_EXAMPLE_REQUEST = re.compile(
    r"\b(?P<negated>do not |don't |skip )?(?:update|edit|modify) (?:the )?"
    r"(?:example (?:environment|env) file|(?:environment|env) (?:example|template)(?: file)?)"
    r"(?P<conditional> if (?:it|one) exists)?\b",
    re.IGNORECASE,
)
_ENV_TEMPLATE = re.compile(r"\.env[._-](?:example|sample|template|dist)", re.IGNORECASE)


def requested_paths(
    task: str, tracked: set[str], prior: dict[str, str] | None = None
) -> dict[str, str]:
    """Recognize explicit edit commands and a tracked, unambiguous environment template."""
    requirements = dict(prior or {})
    for match in _FILE_REQUEST.finditer(task):
        path = match["path"]
        if (
            (not match["quote"] and "/" not in path and "." not in path)
            or len(path) > 1024
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            continue
        if match["negated"]:
            requirements.pop(path, None)
        else:
            requirements[path] = _ACTIONS[match["verb"].lower()]
    templates = sorted(path for path in tracked if _ENV_TEMPLATE.fullmatch(path.rsplit("/", 1)[-1]))
    root_templates = [path for path in templates if "/" not in path]
    templates = root_templates or templates
    if len(templates) == 1:
        for match in _EXAMPLE_REQUEST.finditer(task):
            if match["negated"]:
                requirements.pop(templates[0], None)
            elif match["conditional"]:
                requirements[templates[0]] = "MODIFY"
    return dict(list(requirements.items())[:10])
