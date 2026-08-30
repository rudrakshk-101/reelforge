"""Entry points for the GitHub Actions (serverless) deployment.

  reelforge-cloud process --url <youtube-url>
      ingest -> transcribe -> highlight -> render, upload each clip to YouTube as
      UNLISTED, then send it to Telegram with Approve / Reject buttons.

  reelforge-cloud decide --action approve|reject --video-id <id>
                         --chat <chat_id> --message <message_id>
      approve -> flip the YouTube video to public
      reject  -> delete the YouTube video
      then edit the Telegram message to show the outcome.

There is no always-on process. The Cloudflare Worker turns Telegram events into
`repository_dispatch` calls that run these commands.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import notify, pipeline, publish_youtube
from .config import get_config
from .store import Store


def _process(url: str) -> int:
    cfg = get_config()
    tok, chat = cfg.secrets.telegram_bot_token, cfg.secrets.telegram_chat_id
    store = Store(cfg.db_file)
    job_id = store.add_job(url)
    if job_id is None:
        existing = store.get_job_by_url(url)
        if existing and existing["status"] == "done":
            store.close()
            notify.send_message(tok, chat, f"Already done: {url}")
            return 0
        # a previous attempt failed or is stuck — retry it
        job_id = existing["id"]
        store.update_job(job_id, status="queued", error=None)

    if store.uploaded_in_last_24h() >= cfg["daily_post_cap"]:
        store.update_job(job_id, status="error", error="daily cap reached")
        store.close()
        notify.send_message(
            tok, chat, f"Daily cap ({cfg['daily_post_cap']}) reached — skipped {url}"
        )
        return 0
    store.close()

    try:
        clip_ids = pipeline.prepare_job(job_id, cfg)
    except Exception as exc:  # noqa: BLE001
        notify.send_message(tok, chat, f"❌ Job {job_id} failed:\n{exc}")
        return 1

    store = Store(cfg.db_file)
    try:
        for cid in clip_ids:
            row = store.clip(cid)
            if row["yt_video_id"]:
                continue
            res = publish_youtube.upload_short(
                Path(row["render_path"]),
                title=row["hook_title"] or "",
                description=row["caption"] or "",
                hashtags=(row["hashtags"] or "").split(","),
                client_secrets=cfg.secrets.youtube_client_secrets_file,
                token_file=cfg.secrets.youtube_token_file,
                privacy_status="unlisted",
                category_id=str(cfg["youtube"]["category_id"]),
                made_for_kids=cfg["youtube"]["made_for_kids"],
            )
            store.update_clip(
                cid, yt_video_id=res["video_id"], yt_url=res["url"], status="uploaded"
            )
            caption = (
                f"{row['hook_title']}\n\n{row['caption']}\n{row['hashtags'] or ''}\n\n"
                f"Uploaded unlisted — Approve to make public."
            )
            notify.send_clip_for_approval(
                tok, chat, Path(row["render_path"]), caption, res["video_id"]
            )
        store.update_job(job_id, status="done")
    finally:
        store.close()
    return 0


def _decide(action: str, video_id: str, chat_id: str, message_id: int) -> int:
    cfg = get_config()
    tok = cfg.secrets.telegram_bot_token
    store = Store(cfg.db_file)
    row = store.clip_by_yt_id(video_id)

    try:
        if action == "approve":
            if store.published_in_last_24h("youtube") >= cfg["daily_post_cap"]:
                notify.edit_caption(
                    tok, chat_id, message_id,
                    "⚠️ Daily post cap reached — left as unlisted. Approve again tomorrow.",
                )
                return 0
            publish_youtube.set_privacy(
                video_id,
                "public",
                cfg.secrets.youtube_client_secrets_file,
                cfg.secrets.youtube_token_file,
                made_for_kids=cfg["youtube"]["made_for_kids"],
            )
            if row:
                store.update_clip(
                    row["id"], status="published", published_at=time.time()
                )
            notify.edit_caption(
                tok, chat_id, message_id,
                f"✅ Posted\nhttps://youtube.com/shorts/{video_id}",
            )
        else:
            publish_youtube.delete_video(
                video_id,
                cfg.secrets.youtube_client_secrets_file,
                cfg.secrets.youtube_token_file,
            )
            if row:
                store.update_clip(row["id"], status="rejected")
            notify.edit_caption(tok, chat_id, message_id, "❌ Rejected and deleted")
    except Exception as exc:  # noqa: BLE001
        notify.edit_caption(
            tok, chat_id, message_id, f"⚠️ {action} failed: {exc}"
        )
        store.close()
        return 1
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="reelforge-cloud")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("process")
    pp.add_argument("--url", required=True)

    pd = sub.add_parser("decide")
    pd.add_argument("--action", required=True, choices=["approve", "reject"])
    pd.add_argument("--video-id", required=True)
    pd.add_argument("--chat", required=True)
    pd.add_argument("--message", required=True, type=int)

    a = p.parse_args(argv)
    if a.cmd == "process":
        return _process(a.url)
    return _decide(a.action, a.video_id, a.chat, a.message)


if __name__ == "__main__":
    sys.exit(main())
