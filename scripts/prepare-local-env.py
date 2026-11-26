"""Create local settings once, keeping the generated keys out of terminal output."""

import os
import secrets
from pathlib import Path

# Each gets 32 random bytes in URL-safe base64: the OAuth state signing key and the key that
# encrypts signed-in users' GitHub tokens.
GENERATED_KEYS = ("REFACTRA_OAUTH_STATE_SECRET=", "REFACTRA_TOKEN_ENCRYPTION_KEY=")


def prepare_local_env(root: Path) -> bool:
    destination = root / ".env"
    if destination.exists():
        print(
            ".env already exists; left unchanged. Add any missing settings from .env.example."
        )
        return False
    content = (root / ".env.example").read_text(encoding="utf-8")
    for placeholder in GENERATED_KEYS:
        if content.count(f"\n{placeholder}\n") != 1:
            raise ValueError(f"Expected exactly one empty {placeholder} line in .env.example.")
        content = content.replace(
            f"\n{placeholder}\n", f"\n{placeholder}{secrets.token_urlsafe(32)}\n", 1
        )
    # Exclusive creation prevents overwriting a file created concurrently. 0600 also
    # restricts the file to its owner on POSIX hosts; Windows uses the folder's ACL.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(content)
    print(
        "Created ignored .env with random signing and encryption keys. "
        "Fill GitHub Client ID/secret locally."
    )
    return True


if __name__ == "__main__":
    prepare_local_env(Path(__file__).resolve().parents[1])
