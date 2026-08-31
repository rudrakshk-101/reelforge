"""Publish a rendered clip as an Instagram Reel via the Graph API.

The Graph API needs the video at a public HTTPS URL. Instead of a bucket, we upload
the clip to litterbox (catbox.moe's temp host — no account, files auto-expire after
1h), hand Instagram that URL, and let it expire on its own.

  1. upload mp4 to litterbox -> https URL
  2. POST /{ig_user_id}/media  (media_type=REELS, video_url, caption)
  3. poll GET /{container_id}?fields=status_code until FINISHED
  4. POST /{ig_user_id}/media_publish (creation_id=container_id)
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

GRAPH = "https://graph.facebook.com"
LITTERBOX = "https://litterbox.catbox.moe/resources/internals/api.php"


def _upload_temp(video_path: Path) -> str:
    with open(video_path, "rb") as fh:
        r = requests.post(
            LITTERBOX,
            data={"reqtype": "fileupload", "time": "1h"},
            files={"fileToUpload": (video_path.name, fh, "video/mp4")},
            timeout=180,
        )
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"litterbox upload failed: {url[:200]}")
    return url


def publish_reel(
    video_path: Path,
    caption: str,
    hashtags: list[str],
    ig_user_id: str,
    access_token: str,
    graph_version: str,
    poll_timeout_s: int = 300,
) -> dict:
    if not ig_user_id or not access_token:
        raise RuntimeError("Instagram not configured (IG_USER_ID / IG_ACCESS_TOKEN)")

    public_url = _upload_temp(video_path)
    full_caption = (
        caption + "\n\n" + " ".join(f"#{h.lstrip('#')}" for h in hashtags)
    ).strip()

    create = requests.post(
        f"{GRAPH}/{graph_version}/{ig_user_id}/media",
        data={
            "media_type": "REELS",
            "video_url": public_url,
            "caption": full_caption,
            "access_token": access_token,
        },
        timeout=60,
    )
    create.raise_for_status()
    container_id = create.json()["id"]

    deadline = time.time() + poll_timeout_s
    while time.time() < deadline:
        st = requests.get(
            f"{GRAPH}/{graph_version}/{container_id}",
            params={"fields": "status_code", "access_token": access_token},
            timeout=30,
        ).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError(f"IG container processing failed: {st}")
        time.sleep(5)
    else:
        raise TimeoutError("IG container did not finish processing in time")

    pub = requests.post(
        f"{GRAPH}/{graph_version}/{ig_user_id}/media_publish",
        data={"creation_id": container_id, "access_token": access_token},
        timeout=60,
    )
    pub.raise_for_status()
    media_id = pub.json()["id"]

    perma = requests.get(
        f"{GRAPH}/{graph_version}/{media_id}",
        params={"fields": "permalink", "access_token": access_token},
        timeout=30,
    ).json()
    return {"media_id": media_id, "url": perma.get("permalink", "")}
