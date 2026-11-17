"""Create local settings once, keeping the generated signing key out of terminal output."""

import os
import secrets
from pathlib import Path


def prepare_local_env(root: Path) -> bool:
    destination = root / ".env"
    if destination.exists():
        print(
            ".env already exists; left unchanged. Add any missing settings from .env.example."
        )
        return False
    template = (root / ".env.example").read_text(encoding="utf-8")
    placeholder = "REFACTRA_OAUTH_STATE_SECRET="
    if template.count(placeholder) != 1:
        raise ValueError(
            "Expected exactly one signing-key placeholder in .env.example."
        )
    content = template.replace(placeholder, placeholder + secrets.token_urlsafe(32), 1)
    # Exclusive creation prevents overwriting a file created concurrently. 0600 also
    # restricts the file to its owner on POSIX hosts; Windows uses the folder's ACL.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(content)
    print(
        "Created ignored .env with a random signing key. Fill GitHub Client ID/secret locally."
    )
    return True


if __name__ == "__main__":
    prepare_local_env(Path(__file__).resolve().parents[1])
