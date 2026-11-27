"""Where the report job keeps its files: a local folder or `gs://bucket[/prefix]`.

Layout under the root (a `dry-run/` prefix goes in front of all of it for a dry run):

    raw/<run date>/...                the extraction (Parquet tables, manifest, raw JSON)
    reports/<report>/<anchor>/...     the outputs of a report (`delivery.py`)
    sent/<report>/<anchor>.json       the sent-marker
    reports/<anchor>/, sent/<anchor>.json   the same, by the weekly job before the cut-over
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Store(Protocol):
    def url(self, key: str) -> str: ...

    def exists(self, key: str) -> bool: ...

    def read_text(self, key: str) -> str: ...

    def write_text(self, key: str, text: str) -> None: ...

    def upload(self, key: str, path: Path) -> None: ...

    def child(self, prefix: str) -> Store: ...


def _join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p.strip("/"))


class LocalStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def url(self, key: str) -> str:
        return str(self.root / key) if key else str(self.root)

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()

    def read_text(self, key: str) -> str:
        return (self.root / key).read_text()

    def write_text(self, key: str, text: str) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def upload(self, key: str, path: Path) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(path).read_bytes())

    def child(self, prefix: str) -> LocalStore:
        return LocalStore(self.root / prefix)


class GcsStore:
    def __init__(self, bucket: str, prefix: str = "", client=None) -> None:
        if client is None:
            from google.cloud import storage  # needs the `gcp` extra

            client = storage.Client()
        self.client = client
        self.bucket_name = bucket
        self.bucket = client.bucket(bucket)
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return _join(self.prefix, key)

    def url(self, key: str) -> str:
        return f"gs://{_join(self.bucket_name, self.prefix, key)}"

    def exists(self, key: str) -> bool:
        return self.bucket.blob(self._key(key)).exists()

    def read_text(self, key: str) -> str:
        return self.bucket.blob(self._key(key)).download_as_text()

    def write_text(self, key: str, text: str) -> None:
        self.bucket.blob(self._key(key)).upload_from_string(text, content_type="application/json")

    def upload(self, key: str, path: Path) -> None:
        self.bucket.blob(self._key(key)).upload_from_filename(str(path))

    def child(self, prefix: str) -> GcsStore:
        return GcsStore(self.bucket_name, _join(self.prefix, prefix), self.client)


def make_store(root: str) -> Store:
    if root.startswith("gs://"):
        bucket, _, prefix = root[len("gs://") :].partition("/")
        return GcsStore(bucket, prefix)
    return LocalStore(root)
