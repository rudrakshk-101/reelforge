"""End-to-end orchestration for a single source URL.

Two ways to drive it:
  * ``run_job``      — CLI / scheduler: does everything including publish per publish_mode.
  * ``prepare_job``  — the Telegram bot: ingest -> transcribe -> highlight -> render only,
                       returns the rendered clip ids. The bot then handles approval and
                       calls ``publish_clip`` per clip.
"""

from __future__ import annotations

import logging
import time
import traceback
from pathlib import Path

import shutil

from . import highlight, ingest, notify, publish_instagram, publish_youtube, render, transcribe
from .config import Config, get_config
from .store import Store

log = logging.getLogger("reelforge.pipeline")


def _job_dir(cfg: Config, job_id: int, url: str | None = None) -> Path:
    """Per-job working dir. If `url` is given and the dir holds a *different*
    URL's cached artifacts (job ids can repeat after a DB reset), wipe it first."""
    d = cfg.jobs_dir / f"job_{job_id:05d}"
    marker = d / "url.txt"
    if url is not None and d.exists():
        if not marker.exists() or marker.read_text(encoding="utf-8").strip() != url.strip():
            shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    if url is not None:
        marker.write_text(url.strip(), encoding="utf-8")
    return d


# --------------------------------------------------------------------------
# stages 1-4: fetch + render, no publishing
# --------------------------------------------------------------------------
def prepare_job(job_id: int, cfg: Config | None = None) -> list[int]:
    """Run ingest -> transcribe -> highlight -> render. Returns rendered clip ids."""
    cfg = cfg or get_config()
    store = Store(cfg.db_file)
    try:
        job = store._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise ValueError(f"no job {job_id}")
        url = job["source_url"]
        jd = _job_dir(cfg, job_id, url)
        log.info("job %s: %s", job_id, url)

        meta = ingest.ingest(url, jd)
        store.update_job(
            job_id,
            title=meta.title,
            channel=meta.channel,
            duration_sec=meta.duration_sec,
            status="ingested",
        )

        tr = transcribe.transcribe(meta.video_path, jd, cfg["whisper"])
        store.update_job(job_id, status="transcribed")

        clips = highlight.select_highlights(
            tr,
            jd,
            source_title=meta.title,
            llm_cfg=cfg["llm"],
            n=cfg["clips_per_video"],
            min_s=cfg["clip_min_seconds"],
            max_s=cfg["clip_max_seconds"],
            api_key=cfg.secrets.gemini_api_key,
        )
        store.update_job(job_id, status="highlighted")

        rendered_ids: list[int] = []
        for i, clip in enumerate(clips):
            hashtags = clip.get("hashtags", [])
            if isinstance(hashtags, str):
                hashtags = [hashtags]
            clip_id = store.upsert_clip(
                job_id,
                idx=i,
                start_sec=float(clip["start"]),
                end_sec=float(clip["end"]),
                hook_title=clip.get("hook_title", ""),
                caption=clip.get("caption", ""),
                hashtags=",".join(hashtags),
            )
            existing = store.clip(clip_id)
            if existing["render_path"] and Path(existing["render_path"]).exists():
                rendered_ids.append(clip_id)
                continue
            rc = render.render_clip(
                idx=i,
                clip=clip,
                source_video=meta.video_path,
                words=tr["words"],
                job_dir=jd,
                render_cfg=cfg["render"],
                max_original_seconds=cfg["max_original_seconds"],
            )
            store.update_clip(clip_id, render_path=str(rc.path), status="rendered")
            rendered_ids.append(clip_id)
        store.update_job(job_id, status="rendered")
        return rendered_ids
    except Exception as exc:  # noqa: BLE001
        log.error("job %s failed in prepare: %s", job_id, exc)
        store.update_job(job_id, status="error", error=f"{exc}\n{traceback.format_exc()}")
        raise
    finally:
        store.close()


# --------------------------------------------------------------------------
# publish one already-rendered clip
# --------------------------------------------------------------------------
def _publish_clip(cfg: Config, store: Store, clip_row) -> dict:
    plats = cfg["platforms"]
    video = Path(clip_row["render_path"])
    hashtags = (clip_row["hashtags"] or "").split(",") if clip_row["hashtags"] else []
    hashtags = [h for h in (t.strip() for t in hashtags) if h]

    updates: dict = {}
    if plats.get("youtube") and not clip_row["yt_video_id"]:
        if store.published_in_last_24h("youtube") >= cfg["daily_post_cap"]:
            raise RuntimeError("YouTube daily post cap reached")
        res = publish_youtube.upload_short(
            video,
            title=clip_row["hook_title"] or "",
            description=clip_row["caption"] or "",
            hashtags=hashtags,
            client_secrets=cfg.secrets.youtube_client_secrets_file,
            token_file=cfg.secrets.youtube_token_file,
            privacy_status=cfg["youtube"]["privacy_status"],
            category_id=str(cfg["youtube"]["category_id"]),
            made_for_kids=cfg["youtube"]["made_for_kids"],
        )
        updates.update(yt_video_id=res["video_id"], yt_url=res["url"])

    if plats.get("instagram") and not clip_row["ig_media_id"]:
        if store.published_in_last_24h("instagram") >= cfg["daily_post_cap"]:
            raise RuntimeError("Instagram daily post cap reached")
        res = publish_instagram.publish_reel(
            video,
            caption=clip_row["caption"] or "",
            hashtags=hashtags,
            ig_user_id=cfg.secrets.ig_user_id,
            access_token=cfg.secrets.ig_access_token,
            graph_version=cfg.secrets.ig_graph_version,
            r2_endpoint=cfg.secrets.r2_endpoint,
            r2_bucket=cfg.secrets.r2_bucket,
            r2_access_key=cfg.secrets.r2_access_key,
            r2_secret_key=cfg.secrets.r2_secret_key,
            r2_public_base=cfg.secrets.r2_public_base,
        )
        updates.update(ig_media_id=res["media_id"], ig_url=res["url"])

    if updates:
        updates["status"] = "published"
        updates["published_at"] = time.time()
        store.update_clip(clip_row["id"], **updates)
    return updates


def publish_clip(clip_id: int, cfg: Config | None = None) -> dict:
    cfg = cfg or get_config()
    store = Store(cfg.db_file)
    try:
        row = store.clip(clip_id)
        if row is None:
            raise ValueError(f"no clip {clip_id}")
        if row["status"] == "published":
            return {}
        return _publish_clip(cfg, store, row)
    finally:
        store.close()


def delete_clip_video(clip_id: int, cfg: Config | None = None) -> None:
    """Reject: remove the local render, and delete the video from YouTube if uploaded."""
    cfg = cfg or get_config()
    store = Store(cfg.db_file)
    try:
        row = store.clip(clip_id)
        if row is None:
            return
        if row["yt_video_id"]:
            try:
                publish_youtube.delete_video(
                    row["yt_video_id"],
                    cfg.secrets.youtube_client_secrets_file,
                    cfg.secrets.youtube_token_file,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("could not delete YT video %s: %s", row["yt_video_id"], exc)
        if row["render_path"]:
            Path(row["render_path"]).unlink(missing_ok=True)
        store.update_clip(clip_id, status="rejected")
    finally:
        store.close()


# --------------------------------------------------------------------------
# CLI / scheduler path: prepare + publish per publish_mode
# --------------------------------------------------------------------------
def run_job(job_id: int, cfg: Config | None = None) -> None:
    cfg = cfg or get_config()
    try:
        rendered_ids = prepare_job(job_id, cfg)
    except Exception as exc:  # noqa: BLE001
        if cfg.secrets.telegram_bot_token:
            notify.send_message(
                cfg.secrets.telegram_bot_token,
                cfg.secrets.telegram_chat_id,
                f"reelforge: job {job_id} FAILED — {exc}",
            )
        raise

    store = Store(cfg.db_file)
    try:
        mode = cfg["publish_mode"]
        for clip_id in rendered_ids:
            row = store.clip(clip_id)
            if row["status"] == "published":
                continue
            if mode == "off":
                continue
            if mode == "telegram":
                timeout_s = int(
                    cfg.get("telegram", {}).get("approval_timeout_minutes", 360) * 60
                )
                approved = notify.request_approval(
                    cfg.secrets.telegram_bot_token,
                    cfg.secrets.telegram_chat_id,
                    clip_id,
                    Path(row["render_path"]),
                    caption=f"{row['hook_title']}\n{row['caption']}",
                    timeout_s=timeout_s,
                )
                store.update_clip(clip_id, status="approved" if approved else "rejected")
                if not approved:
                    continue
            _publish_clip(cfg, store, store.clip(clip_id))

        store.update_job(job_id, status="done", error=None)
        if cfg.secrets.telegram_bot_token:
            done = [store.clip(c) for c in rendered_ids]
            links = [d["yt_url"] or d["ig_url"] for d in done if d["yt_url"] or d["ig_url"]]
            notify.send_message(
                cfg.secrets.telegram_bot_token,
                cfg.secrets.telegram_chat_id,
                f"reelforge: job {job_id} done — {len(links)} published\n" + "\n".join(links),
            )
    finally:
        store.close()
