"""Accept the pinned public-wheel subset of pip-style lockfiles."""

import re

PYTHON_LOCK_PATH = "requirements.lock"
_MAX_BYTES = 64 * 1024
_MAX_REQUIREMENTS = 128
_PINNED = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.!+-]*"
    r"(?:[ \t]+--hash=sha256:[0-9a-f]{64})+"
)


class PythonLockError(ValueError):
    """The lockfile cannot be used to prepare an offline environment."""


def pinned_python_requirements(lock: bytes) -> bytes:
    if len(lock) > _MAX_BYTES:
        raise PythonLockError("requirements.lock exceeds the dependency preparation limit.")
    try:
        body = lock.decode("ascii").replace("\r\n", "\n").replace("\\\n", " ")
    except UnicodeDecodeError:
        raise PythonLockError("requirements.lock must be an ASCII wheel lock.") from None
    requirements = []
    for line in body.splitlines():
        requirement = line.partition("#")[0].strip()
        if not requirement:
            continue
        if not _PINNED.fullmatch(requirement):
            raise PythonLockError(
                "requirements.lock needs exact package versions and SHA-256 hashes "
                "for public wheels."
            )
        requirements.append(" ".join(requirement.split()))
    if not 1 <= len(requirements) <= _MAX_REQUIREMENTS:
        raise PythonLockError("requirements.lock needs between 1 and 128 pinned packages.")
    return ("\n".join(requirements) + "\n").encode("ascii")
