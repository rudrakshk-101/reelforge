"""Stage 1: download the source video + metadata.

Two download backends:
  * VidKraken API  — used when VIDKRAKEN_KEY is set. Works from any IP (they run the
    proxying), so this is the cloud / GitHub Actions path.
  * yt-dlp         — the local fallback (home IP). Also the fallback if VidKraken fails.

Produces, inside the job dir:
  source.mp4        — the downloaded video
  source.info.json  — metadata (small dict)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import yt_dlp

_ROOT = Path(__file__).resolve().parents[2]

VIDKRAKEN_KEY = os.environ.get("VIDKRAKEN_KEY", "").strip()
VIDKRAKEN_BASE = "https://vidkraken.com/api/v2"
# 720 is a good balance for a 9:16 crop; drop to 480 to save API bandwidth.
VIDKRAKEN_FORMAT = os.environ.get("VIDKRAKEN_FORMAT", "720")

_COOKIES = Path(os.environ.get("YOUTUBE_COOKIES_FILE", "secrets/youtube_cookies.txt"))
if not _COOKIES.is_absolute():
    _COOKIES = _ROOT / _COOKIES


@dataclass
class SourceMeta:
    video_path: Path
    info_path: Path
    title: str
    channel: str
    duration_sec: float
    subtitle_path: Path | None


# --------------------------------------------------------------------------
# backend 1: VidKraken (cloud-friendly)
# --------------------------------------------------------------------------
def _oembed(url: str) -> dict:
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=15,
        )
        if r.ok:
            return r.json()
    except Exception:
        pass
    return {}


def _vidkraken(url: str, job_dir: Path) -> SourceMeta:
    hdr = {"Authorization": f"Bearer {VIDKRAKEN_KEY}"}
    sub = requests.post(
        f"{VIDKRAKEN_BASE}/download",
        headers={**hdr, "Content-Type": "application/json"},
        json={"url": url, "format": VIDKRAKEN_FORMAT},
        timeout=30,
    )
    sub.raise_for_status()
    j = sub.json()
    job_id = j["jobId"]
    title = j.get("title") or _oembed(url).get("title") or url
    duration = float(j.get("duration") or 0.0)

    download_url = None
    deadline = time.time() + 300
    while time.time() < deadline:
        st = requests.get(
            f"{VIDKRAKEN_BASE}/download/{job_id}", headers=hdr, timeout=30
        ).json()
        status = (st.get("status") or "").upper()
        duration = float(st.get("duration") or duration)
        if status == "COMPLETED":
            download_url = st.get("downloadUrl")
            break
        if status in ("FAILED", "ERROR"):
            raise RuntimeError(f"VidKraken job failed: {st}")
        time.sleep(4)
    if not download_url:
        raise TimeoutError("VidKraken job did not complete in time")

    out = job_dir / "source.mp4"
    with requests.get(download_url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(out, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    if out.stat().st_size < 20000:
        raise RuntimeError("VidKraken download produced a tiny/empty file")

    meta = _oembed(url)
    info = {
        "title": title,
        "channel": meta.get("author_name") or "",
        "duration": duration or _ffprobe_duration(out),
        "backend": "vidkraken",
    }
    (job_dir / "source.info.json").write_text(json.dumps(info), encoding="utf-8")
    return SourceMeta(
        video_path=out,
        info_path=job_dir / "source.info.json",
        title=info["title"],
        channel=info["channel"],
        duration_sec=info["duration"],
        subtitle_path=None,
    )


def _ffprobe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip() or 0.0)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------
# backend 2: yt-dlp (home IP)
# --------------------------------------------------------------------------
def _js_runtimes() -> dict[str, dict]:
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


def _ytdlp(url: str, job_dir: Path) -> SourceMeta:
    proxy = os.environ.get("PROXY_URL", "").strip()
    max_h = 720 if proxy else 1080

    ydl_opts = {
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "format": f"bv*[height<={max_h}]+ba/b[height<={max_h}]/bv*+ba/b/best",
        "merge_output_format": "mp4",
        "writeinfojson": True,
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
        "extractor_args": {
            "youtube": {"player_client": ["default", "web_safari", "mweb"]}
        },
    }
    if _COOKIES.exists() and _COOKIES.stat().st_size > 0:
        ydl_opts["cookiefile"] = str(_COOKIES)
    if proxy:
        ydl_opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    video_path = job_dir / "source.mp4"
    if not video_path.exists():
        cands = [
            c for c in sorted(job_dir.glob("source.*"))
            if c.suffix not in {".json", ".vtt"}
        ]
        if not cands:
            raise FileNotFoundError(f"yt-dlp produced no video file in {job_dir}")
        video_path = cands[0]

    return SourceMeta(
        video_path=video_path,
        info_path=job_dir / "source.info.json",
        title=info.get("title") or url,
        channel=info.get("channel") or info.get("uploader") or "",
        duration_sec=float(info.get("duration") or 0.0),
        subtitle_path=None,
    )


def ingest(url: str, job_dir: Path) -> SourceMeta:
    job_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    if VIDKRAKEN_KEY:
        try:
            return _vidkraken(url, job_dir)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"vidkraken: {exc}")
            for f in job_dir.glob("source.*"):
                f.unlink(missing_ok=True)
    try:
        return _ytdlp(url, job_dir)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"yt-dlp: {exc}")
        raise RuntimeError("download failed — " + " | ".join(errors))
