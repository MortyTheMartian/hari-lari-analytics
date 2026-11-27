"""Where the Telegram credentials come from.

In Cloud Run the three secrets arrive as environment variables (`--set-secrets`). On a laptop
they are read from Secret Manager through the `gcloud` CLI, so no secret ever sits in a file.
"""

from __future__ import annotations

import os
import subprocess

# environment variable, Secret Manager secret
BOT_TOKEN = ("TELEGRAM_BOT_TOKEN", "telegram-bot-token")
CHAT_ID = ("TELEGRAM_CHAT_ID", "telegram-chat-id")
ADMIN_CHAT_ID = ("TELEGRAM_ADMIN_CHAT_ID", "telegram-admin-chat-id")
# Google OAuth client and the user's refresh token for the Drive upload (one set for all jobs)
DRIVE_CLIENT_ID = ("DRIVE_OAUTH_CLIENT_ID", "drive-oauth-client-id")
DRIVE_CLIENT_SECRET = ("DRIVE_OAUTH_CLIENT_SECRET", "drive-oauth-client-secret")
DRIVE_REFRESH_TOKEN = ("DRIVE_OAUTH_REFRESH_TOKEN", "drive-oauth-refresh-token")


class MissingSecret(RuntimeError):
    pass


def get_secret(spec: tuple[str, str], project: str | None = None) -> str:
    env_name, secret = spec
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    project = project or os.environ.get("GCP_PROJECT") or "hari-lari"
    try:
        out = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest", "--secret", secret,
             "--project", project],
            capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MissingSecret(f"{env_name} is not set and gcloud failed: {error}") from None
    value = out.stdout.strip()
    if out.returncode != 0 or not value:
        raise MissingSecret(
            f"{env_name} is not set and secret '{secret}' has no value in project {project}"
        )
    return value
