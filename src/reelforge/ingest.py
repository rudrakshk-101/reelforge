"""Stage 1: download the source video + metadata with yt-dlp.

Produces, inside the job dir:
  source.mp4        — best <=1080p mp4 (re-muxed if needed)
  source.info.json  — yt-dlp metadata dump
  subs.*.vtt        — creator-supplied subtitles, if any (used to speed transcription)
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

# Datacenter IPs (GitHub Actions) get bot-walled by YouTube. A cookies.txt from a
# logged-in session (use a throwaway Google account) gets past it.
_COOKIES = Path(
    os.environ.get("YOUTUBE_COOKIES_FILE", "secrets/youtube_cookies.txt")
)
if not _COOKIES.is_absolute():
    _COOKIES = Path(__file__).resolve().parents[2] / _COOKIES


def _js_runtimes() -> dict[str, dict]:
    """yt-dlp now needs a JS runtime for YouTube. Prefer deno, fall back to node.

    Pass an explicit binary path so it works even when PATH wasn't refreshed after
    installing the runtime (common on Windows). Format is a dict:
    ``{"deno": {"path": "..."}, "node": {}}``.
    """
    # Extra spots to look on Windows when PATH wasn't refreshed after a winget install.
    extra = {
        "deno": [
            Path.home()
            / "AppData/Local/Microsoft/WinGet/Packages"
            / "DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe/deno.exe",
        ],
        "node": [Path(r"C:/Program Files/nodejs/node.exe")],
    }
    runtimes: dict[str, dict] = {}
    for name in ("deno", "node"):
        found = shutil.which(name) or next(
            (str(p) for p in extra.get(name, []) if p.exists()), None
        )
        runtimes[name] = {"path": found} if found else {}
    return runtimes


@dataclass
class SourceMeta:
    video_path: Path
    info_path: Path
    title: str
    channel: str
    duration_sec: float
    subtitle_path: Path | None


def ingest(url: str, job_dir: Path) -> SourceMeta:
    job_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(job_dir / "source.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "writeinfojson": True,
        # Subtitles are best-effort only (Whisper does the real transcription) and
        # YouTube 429s them aggressively, so don't let them fail the job.
        "writesubtitles": False,
        "writeautomaticsub": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "js_runtimes": _js_runtimes(),
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "sleep_interval_requests": 1,
        "ignoreerrors": False,
        # web_safari works with just cookies (no PO token); mweb/tv are fallbacks.
        "extractor_args": {
            "youtube": {"player_client": ["web_safari", "mweb", "tv"]}
        },
    }
    if _COOKIES.exists() and _COOKIES.stat().st_size > 0:
        ydl_opts["cookiefile"] = str(_COOKIES)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    video_path = job_dir / "source.mp4"
    if not video_path.exists():
        # fall back to whatever extension landed
        cands = sorted(job_dir.glob("source.*"))
        cands = [c for c in cands if c.suffix not in {".json", ".vtt"}]
        if not cands:
            raise FileNotFoundError(f"yt-dlp produced no video file in {job_dir}")
        video_path = cands[0]

    info_path = job_dir / "source.info.json"
    if not info_path.exists():
        info_path.write_text(json.dumps(info), encoding="utf-8")

    subs = sorted(job_dir.glob("source*.vtt"))
    subtitle_path = subs[0] if subs else None

    return SourceMeta(
        video_path=video_path,
        info_path=info_path,
        title=info.get("title") or url,
        channel=info.get("channel") or info.get("uploader") or "",
        duration_sec=float(info.get("duration") or 0.0),
        subtitle_path=subtitle_path,
    )
