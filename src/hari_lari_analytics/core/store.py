"""Where the jobs keep their files: a local folder or `gs://bucket[/prefix]`.

The layout under the root is in docs/architecture.md ("Storage layout"); a `dry-run/` prefix goes
in front of all of it for a dry run. Until the cut-over of phase B the report job also writes
`raw/<run date>/` (the extraction), `reports/<report>/<anchor>/` and the sent-markers
`sent/<report>/<anchor>.json` (`sent/<anchor>.json` for the weekly job before 9.7).
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

    def download(self, key: str, path: Path) -> None: ...

    def list(self, prefix: str = "") -> list[str]:
        """Every key under the folder `prefix` (all keys for ""), sorted, relative to the root."""
        ...

    def delete(self, key: str) -> None:
        """Delete one key; a missing key is not an error."""
        ...

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

    def download(self, key: str, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes((self.root / key).read_bytes())

    def list(self, prefix: str = "") -> list[str]:
        base = self.root / prefix if prefix else self.root
        if not base.is_dir():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file())

    def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)

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

    def download(self, key: str, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.bucket.blob(self._key(key)).download_to_filename(str(path))

    def list(self, prefix: str = "") -> list[str]:
        folder = self._key(prefix)
        start = len(self.prefix) + 1 if self.prefix else 0
        blobs = self.client.list_blobs(self.bucket_name, prefix=f"{folder}/" if folder else None)
        return sorted(b.name[start:] for b in blobs if not b.name.endswith("/"))

    def delete(self, key: str) -> None:
        blob = self.bucket.blob(self._key(key))
        if blob.exists():
            blob.delete()

    def child(self, prefix: str) -> GcsStore:
        return GcsStore(self.bucket_name, _join(self.prefix, prefix), self.client)


def make_store(root: str | Path) -> Store:
    root = str(root)
    if root.startswith("gs://"):
        bucket, _, prefix = root[len("gs://") :].partition("/")
        return GcsStore(bucket, prefix)
    return LocalStore(root)
