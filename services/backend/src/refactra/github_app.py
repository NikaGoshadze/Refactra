"""Trusted GitHub App integration. Callers must authorize the user/repository first.

This module is not exposed through API routes. Keep keys and returned tokens in
trusted server memory, outside repository workspaces and persisted run records.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import time

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class GitHubAppError(Exception):
    """Sanitized integration failure; never include credentials or upstream bodies."""


@dataclass(frozen=True)
class InstallationToken:
    value: str = field(repr=False)
    repository_id: int
    expires_at: datetime


@dataclass(frozen=True)
class GitHubAppClient:
    client_id: str
    private_key: bytes = field(repr=False)
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def mint_read_token(self, installation_id: int, repository_id: int) -> InstallationToken:
        """Request contents/metadata read access for exactly one authorized repository."""
        return self._mint_token(installation_id, repository_id, {"contents": "read"})

    def mint_push_token(self, installation_id: int, repository_id: int) -> InstallationToken:
        """Request contents write access for one approved repository."""
        return self._mint_token(installation_id, repository_id, {"contents": "write"})

    def mint_pull_request_token(
        self, installation_id: int, repository_id: int
    ) -> InstallationToken:
        """Request pull request write access for one approved repository."""
        return self._mint_token(installation_id, repository_id, {"pull_requests": "write"})

    def _mint_token(
        self, installation_id: int, repository_id: int, permissions: dict[str, str]
    ) -> InstallationToken:
        if any(type(value) is not int or value <= 0 for value in (installation_id, repository_id)):
            raise ValueError("Positive installation and repository IDs are required.")
        try:
            if not isinstance(self.client_id, str) or not self.client_id.strip():
                raise ValueError("Missing client ID")
            signing_key = serialization.load_pem_private_key(self.private_key, password=None)
            if not isinstance(signing_key, rsa.RSAPrivateKey):
                raise ValueError("An RSA private key is required")
            now = int(time())
            app_jwt = jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": self.client_id},
                signing_key,
                algorithm="RS256",
            )
            permissions = {**permissions, "metadata": "read"}
            with httpx.Client(
                timeout=10.0, follow_redirects=False, trust_env=False, transport=self.transport
            ) as client:
                response = client.post(
                    f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                    headers={
                        "Authorization": f"Bearer {app_jwt}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2026-03-10",
                    },
                    json={"repository_ids": [repository_id], "permissions": permissions},
                )
            if response.status_code != 201:
                raise ValueError("Token request failed")
            data = response.json()
            token = data["token"]
            expires_at = datetime.fromisoformat(data["expires_at"])
            current_time = datetime.fromtimestamp(time(), UTC)
            repositories = data["repositories"]
            if (
                not isinstance(token, str)
                or not token.strip()
                or expires_at.tzinfo is None
                or not current_time < expires_at <= current_time + timedelta(hours=1, seconds=60)
                or data["permissions"] != permissions
                or not isinstance(repositories, list)
                or len(repositories) != 1
                or type(repositories[0]["id"]) is not int
                or repositories[0]["id"] != repository_id
            ):
                raise ValueError("Unexpected token scope or expiry")
            return InstallationToken(token, repository_id, expires_at)
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError):
            raise GitHubAppError("GitHub App authentication could not be completed.") from None
