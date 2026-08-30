"""Command-line entry point.

  reelforge add <url> [<url> ...]   queue one or more source videos
  reelforge run                     process every queued/errored job now
  reelforge watch                   ingest new lines from sources.txt + configured
                                    channels, then run
  reelforge status                  print jobs + clips table
  reelforge retry <job_id>          re-queue a failed job
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import yt_dlp

from .config import get_config
from .pipeline import run_job
from .store import Store


def _setup_logging(cfg) -> None:
    logfile = cfg.logs_dir / f"reelforge_{datetime.now():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(logfile, encoding="utf-8")],
    )


def _add(store: Store, urls: list[str]) -> None:
    for url in urls:
        url = url.strip()
        if not url or url.startswith("#"):
            continue
        jid = store.add_job(url)
        print(f"  {'queued' if jid else 'already known'}: {url}")


def _channel_urls(channel_url: str, lookback_days: int) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    opts = {"quiet": True, "extract_flat": True, "playlistend": 20, "no_warnings": True}
    urls: list[str] = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
        for entry in info.get("entries", []) or []:
            ts = entry.get("timestamp")
            if ts and datetime.fromtimestamp(ts, timezone.utc) < cutoff:
                continue
            if entry.get("url"):
                urls.append(entry["url"])
    return urls


def _watch(cfg, store: Store) -> None:
    wcfg = cfg["watch"]
    src_file = cfg.root / wcfg["sources_file"]
    if src_file.exists():
        _add(store, src_file.read_text(encoding="utf-8").splitlines())
    for ch in wcfg.get("channels", []) or []:
        try:
            _add(store, _channel_urls(ch, wcfg.get("lookback_days", 3)))
        except Exception as exc:  # noqa: BLE001
            print(f"  channel poll failed for {ch}: {exc}")


def _run_all(cfg) -> None:
    store = Store(cfg.db_file)
    pending = store.jobs_by_status("queued", "ingested", "transcribed", "highlighted", "rendered", "error")
    store.close()
    print(f"{len(pending)} job(s) to process")
    for job in pending:
        try:
            run_job(job["id"], cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"  job {job['id']} error: {exc}")


def _status(cfg) -> None:
    store = Store(cfg.db_file)
    for job in store._conn.execute("SELECT * FROM jobs ORDER BY id").fetchall():
        print(f"[{job['id']:>3}] {job['status']:<12} {job['title'][:60]}")
        for c in store.clips_for_job(job["id"]):
            link = c["yt_url"] or c["ig_url"] or ""
            print(f"      clip {c['idx']} {c['status']:<10} {c['hook_title'][:45]} {link}")
    print(
        f"published last 24h — youtube: {store.published_in_last_24h('youtube')}, "
        f"instagram: {store.published_in_last_24h('instagram')}"
    )
    store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reelforge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("urls", nargs="+")
    sub.add_parser("run")
    sub.add_parser("watch")
    sub.add_parser("status")
    sub.add_parser("auth-youtube")
    p_retry = sub.add_parser("retry")
    p_retry.add_argument("job_id", type=int)

    args = parser.parse_args(argv)
    cfg = get_config()
    _setup_logging(cfg)

    if args.cmd == "add":
        store = Store(cfg.db_file)
        _add(store, args.urls)
        store.close()
    elif args.cmd == "run":
        _run_all(cfg)
    elif args.cmd == "watch":
        store = Store(cfg.db_file)
        _watch(cfg, store)
        store.close()
        _run_all(cfg)
    elif args.cmd == "status":
        _status(cfg)
    elif args.cmd == "auth-youtube":
        from . import publish_youtube

        tok = cfg.secrets.youtube_token_file
        if tok.exists():
            tok.unlink()
        publish_youtube._client(cfg.secrets.youtube_client_secrets_file, tok)
        print(f"\nYouTube authorised. Token written to: {tok}")
        print("Paste its contents into the GitHub secret YOUTUBE_TOKEN:")
        print(f"  gh secret set YOUTUBE_TOKEN < \"{tok}\"")
    elif args.cmd == "retry":
        store = Store(cfg.db_file)
        store.update_job(args.job_id, status="queued", error=None)
        store.close()
        run_job(args.job_id, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
