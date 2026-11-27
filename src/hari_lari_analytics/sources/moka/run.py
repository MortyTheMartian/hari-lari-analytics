"""One extraction run: fetch, check, and land it as `landing/moka/<run date>/` (Parquet tables,
gzipped raw JSON, the request log, and the manifest last; `core/landing.py`)."""

from __future__ import annotations

import gzip
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hari_lari_analytics.core.landing import (
    Checks,
    Manifest,
    Window,
    image_tag,
    land,
    new_run_id,
    table_entry,
)
from hari_lari_analytics.core.project import project
from hari_lari_analytics.core.store import Store, make_store
from hari_lari_analytics.sources.moka import fetch
from hari_lari_analytics.sources.moka.checks import run_checks
from hari_lari_analytics.sources.moka.client import MokaClient
from hari_lari_analytics.sources.moka.output import write_run

SOURCE = "moka"
DEFAULT_SINCE = "2026-01-01"


class ExtractionFailed(RuntimeError):
    """Sanity checks failed; nothing is landed."""


@dataclass
class RunResult:
    location: str
    manifest: dict


def local_now() -> datetime:
    """Now in the project's time zone (project.yml)."""
    return project().now()


def run_extraction(
    client: MokaClient,
    out: str | Store,
    *,
    since: str = DEFAULT_SINCE,
    until: str | None = None,
    only: str | None = None,
    now: Callable[[], datetime] = local_now,
    log: Callable[[str], None] = print,
    run_id: str | None = None,
) -> RunResult:
    """Extract and land the run in the landing store `out` (a folder, gs://, or a Store whose
    root is `landing/`): `<out>/moka/<run date>/`."""
    store = make_store(out) if isinstance(out, str) else out
    t_now = now()
    tz = project().tz
    since_at = datetime.fromisoformat(since).replace(tzinfo=tz)
    until_at = datetime.fromisoformat(until).replace(tzinfo=tz) if until else t_now

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        (staging / "raw").mkdir()

        def raw(name: str, obj: object) -> None:
            with gzip.open(staging / "raw" / f"{name}.json.gz", "wt", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)

        client.tokens.ensure_fresh()
        biz, outs = fetch.outlets(client)
        log(f"business {biz}, outlets {outs}")
        tables, kinds = {}, {}
        if only in (None, "catalog"):
            catalog = fetch.build_catalog(client, outs, raw, log)
            tables.update(catalog)
            kinds.update(dict.fromkeys(catalog, "snapshot"))
        if only in (None, "transactions"):
            events = fetch.build_transactions(
                client, outs, since_at.timestamp(), until_at.timestamp(), raw, log
            )
            tables.update(events)
            kinds.update(dict.fromkeys(events, "events"))
        frames = {k: v.to_frame() for k, v in tables.items()}
        for k, df in frames.items():
            log(f"  {k}: {len(df)} rows x {len(df.columns)} cols")

        errors: list[str] = []
        warnings: list[str] = []
        if "transactions" in frames:
            errors, warnings = run_checks(
                frames["transactions"], frames["transaction_lines"], t_now.replace(tzinfo=None)
            )
        if errors:
            raise ExtractionFailed("; ".join(errors))
        write_run(staging, frames, client.log)
        manifest = Manifest(
            source=SOURCE,
            run_date=t_now.date(),
            run_id=run_id or new_run_id(t_now),
            started_at=t_now,
            finished_at=now(),
            image=image_tag(),
            window=Window(since=since_at.isoformat(), until=until_at.isoformat(timespec="seconds")),
            tables={n: table_entry(staging, n, df, kinds[n]) for n, df in frames.items()},
            checks=Checks(errors=errors, warnings=warnings),
            source_info={
                "http_calls": len(client.log),
                "token_expires_at": datetime.fromtimestamp(
                    client.tokens.expires_at(), tz
                ).isoformat(timespec="seconds"),
                "token_refreshed": client.tokens.refreshed,
            },
        )
        location = land(store, manifest, staging)
    return RunResult(location, manifest.model_dump(mode="json"))
