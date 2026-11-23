"""Google Drive upload of the weekly files, over the plain REST API.

The job uploads the PDF and the Excel lines, as the Google user who gave consent once, into a
week folder inside a Drive folder of that user. The week folder inherits the sharing of the
parent, so the link only opens for the named Google accounts.

The session is anything with a `requests`-style `.request(method, url, ...)` method, so the
tests need no network. `drive_session` builds the real one.
"""

from __future__ import annotations

import json
import mimetypes
import uuid
from pathlib import Path
from typing import Protocol

API = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"
TIMEOUT_SECONDS = 120


class DriveError(RuntimeError):
    """A failed Drive call."""


class Session(Protocol):
    def request(self, method: str, url: str, **kwargs): ...


def drive_session(client_id: str, client_secret: str, refresh_token: str) -> Session:
    """A session that acts as the Google user who gave consent (see `hari-lari-drive`).

    Service accounts cannot create files in a normal Drive ("no storage quota"), so the job
    uploads as the user instead. The files are owned by that user and the access token is
    refreshed automatically from the refresh token.
    """
    from google.auth.transport.requests import AuthorizedSession  # needs the `gcp` extra
    from google.oauth2.credentials import Credentials

    credentials = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    return AuthorizedSession(credentials)


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


class DriveUploader:
    def __init__(self, session: Session, root_folder_id: str) -> None:
        if not root_folder_id:
            raise DriveError("The Drive folder id is empty")
        self.session = session
        self.root = root_folder_id

    # ---- low level ---------------------------------------------------------------
    def _call(self, method: str, url: str, **kwargs) -> dict:
        response = self.session.request(method, url, timeout=TIMEOUT_SECONDS, **kwargs)
        if response.status_code >= 300:
            raise DriveError(f"Drive {method} failed ({response.status_code}): {_reason(response)}")
        return response.json()

    def _find(self, parent: str, name: str, *, folder: bool) -> str | None:
        query = f"name = '{_quote(name)}' and '{parent}' in parents and trashed = false"
        query += f" and mimeType {'=' if folder else '!='} '{FOLDER_MIME}'"
        found = self._call(
            "GET",
            API,
            params={
                "q": query,
                "fields": "files(id)",
                "pageSize": 1,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        files = found.get("files", [])
        return files[0]["id"] if files else None

    # ---- folders and files -------------------------------------------------------
    def ensure_folder(self, name: str, parent: str | None = None) -> str:
        parent = parent or self.root
        existing = self._find(parent, name, folder=True)
        if existing:
            return existing
        created = self._call(
            "POST",
            API,
            params={"supportsAllDrives": "true", "fields": "id"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
        )
        return created["id"]

    def upload(self, path: Path, parent: str) -> str:
        """Create the file in `parent`, or replace the content of the file with that name."""
        path = Path(path)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        existing = self._find(parent, path.name, folder=False)
        params = {"supportsAllDrives": "true", "fields": "id"}
        if existing:
            done = self._call(
                "PATCH",
                f"{UPLOAD}/{existing}",
                params={**params, "uploadType": "media"},
                headers={"Content-Type": mime},
                data=path.read_bytes(),
            )
        else:
            body, content_type = _multipart({"name": path.name, "parents": [parent]}, path, mime)
            done = self._call(
                "POST",
                UPLOAD,
                params={**params, "uploadType": "multipart"},
                headers={"Content-Type": content_type},
                data=body,
            )
        return done["id"]

    def upload_week(self, week_name: str, files: list[Path]) -> str:
        """Put the files into the folder `week_name`; returns the link to that folder."""
        week = self.ensure_folder(week_name)
        for path in files:
            self.upload(path, week)
        return folder_url(week)


def _multipart(metadata: dict, path: Path, mime: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    head = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n"
    ).encode()
    return head + path.read_bytes() + f"\r\n--{boundary}--".encode(), (
        f"multipart/related; boundary={boundary}"
    )


def _reason(response) -> str:
    try:
        error = response.json().get("error", {})
        return str(error.get("message") or error)[:300]
    except ValueError:
        return str(getattr(response, "text", ""))[:300]
