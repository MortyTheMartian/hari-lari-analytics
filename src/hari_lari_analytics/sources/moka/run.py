"""One extraction run: fetch, check, write Parquet + manifest."""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hari_lari_analytics.core.project import project
from hari_lari_analytics.sources.moka import fetch
from hari_lari_analytics.sources.moka.checks import run_checks
from hari_lari_analytics.sources.moka.client import MokaClient
from hari_lari_analytics.sources.moka.output import is_gcs, upload_dir, write_run

DEFAULT_SINCE = "2026-01-01"


class ExtractionFailed(RuntimeError):
    """Sanity checks failed; nothing is published."""


@dataclass
class RunResult:
    location: str
    manifest: dict


def local_now() -> datetime:
    """Now in the project's time zone (project.yml)."""
    return project().now()


def run_extraction(
    client: MokaClient,
    out: str,
    *,
    since: str = DEFAULT_SINCE,
    until: str | None = None,
    only: str | None = None,
    now: Callable[[], datetime] = local_now,
    log: Callable[[str], None] = print,
) -> RunResult:
    started = time.time()
    t_now = now()
    tz = project().tz
    since_ts = datetime.fromisoformat(since).replace(tzinfo=tz).timestamp()
    until_ts = t_now.timestamp()
    if until:
        until_ts = datetime.fromisoformat(until).replace(tzinfo=tz).timestamp()
    run_name = t_now.date().isoformat()
    final = f"{out.rstrip('/')}/{run_name}" if is_gcs(out) else str(Path(out) / run_name)

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) if is_gcs(out) else Path(final)
        (staging / "raw").mkdir(parents=True, exist_ok=True)

        def raw(name: str, obj: object) -> None:
            (staging / "raw" / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False))

        client.tokens.ensure_fresh()
        biz, outs = fetch.outlets(client)
        log(f"business {biz}, outlets {outs}")
        tables = {}
        if only in (None, "catalog"):
            tables.update(fetch.build_catalog(client, outs, raw, log))
        if only in (None, "transactions"):
            tables.update(fetch.build_transactions(client, outs, since_ts, until_ts, raw, log))
        frames = {k: v.to_frame() for k, v in tables.items()}
        for k, df in frames.items():
            log(f"  {k}: {len(df)} rows x {len(df.columns)} cols")

        errors: list[str] = []
        warnings: list[str] = []
        if "transactions" in frames:
            errors, warnings = run_checks(
                frames["transactions"], frames["transaction_lines"], t_now.replace(tzinfo=None)
            )
        manifest = build_manifest(frames, client, started, t_now, errors, warnings, since, until)
        if errors:
            raise ExtractionFailed("; ".join(errors))
        write_run(staging, frames, client.log, manifest)
        if is_gcs(out):
            upload_dir(staging, final)
    return RunResult(final, manifest)


def build_manifest(
    frames, client: MokaClient, started: float, now: datetime, errors, warnings, since, until
) -> dict:
    tx = frames.get("transactions")
    first = last = None
    if tx is not None and not tx.empty:
        first, last = tx["created_at"].min(), tx["created_at"].max()
    return {
        "run_at": now.isoformat(timespec="seconds"),
        "since": since,
        "until": until,
        "row_counts": {k: len(v) for k, v in frames.items()},
        "first_sale": first.isoformat() if first is not None else None,
        "last_sale": last.isoformat() if last is not None else None,
        "http_calls": len(client.log),
        "duration_seconds": round(time.time() - started, 1),
        "token_expires_at": datetime.fromtimestamp(
            client.tokens.expires_at(), project().tz
        ).isoformat(timespec="seconds"),
        "token_refreshed": client.tokens.refreshed,
        "errors": errors,
        "warnings": warnings,
    }
