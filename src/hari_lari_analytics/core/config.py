"""Where the business config lives (docs/architecture.md, "Storage layout"): the CSV files the
warehouse reads as seeds (aliases, category groups, calendar events, later margins and targets).

They are private, so they are neither in git nor in the image: `CONFIG_DIR` names a local folder
(default `config/`, git-ignored) or `gs://bucket/prefix` (the job: `gs://hari-lari-data/config`).
`config/examples/` in git has the same columns with demo values.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from hari_lari_analytics.core.store import Store, make_store

ENV_VAR = "CONFIG_DIR"
DEFAULT = "config"
EXAMPLES = "config/examples"
CONFIG_HELP = f"the config CSV folder, or gs://bucket/prefix (default: ${ENV_VAR}, else {DEFAULT})"


class ConfigError(ValueError):
    """The config folder is missing or lacks a required file."""


def config_location(value: str | Path | None = None) -> str:
    """The config location: `value`, else `$CONFIG_DIR`, else `config`."""
    return str(value or os.environ.get(ENV_VAR) or DEFAULT)


@contextmanager
def local_config(
    location: str | Path, required: list[str], store: Store | None = None
) -> Iterator[Path]:
    """A local folder holding the config files of `location`: the folder itself, or a
    temporary copy of the CSV files under a `gs://` location (read through `store` if given).
    Fails, naming the location, when one of the `required` files is not there."""
    location = str(location)
    if location.startswith("gs://"):
        store = store or make_store(location)
        with tempfile.TemporaryDirectory() as tmp:
            for key in store.list():
                if "/" not in key and key.endswith(".csv"):
                    store.download(key, Path(tmp) / key)
            _require(Path(tmp), required, location)
            yield Path(tmp)
        return
    folder = Path(location)
    _require(folder, required, location)
    yield folder


def _require(folder: Path, required: list[str], location: str) -> None:
    missing = [name for name in required if not (folder / name).is_file()]
    if missing:
        raise ConfigError(
            f"the config {location} has no {', '.join(missing)}. Copy the real files there "
            f"(or the demo files of {EXAMPLES}/ for a try), or set {ENV_VAR}"
        )


def file_hashes(folder: Path) -> dict[str, str]:
    """The SHA-256 (first 12 hex digits) of each CSV file in `folder`, for the run ledger."""
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:12]
        for p in sorted(folder.glob("*.csv"))
    }
