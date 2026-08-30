"""Publish a rendered clip as an Instagram Reel via the Graph API.

The Graph API needs the video at a public HTTPS URL, so we:
  1. upload the mp4 to a Cloudflare R2 bucket (S3-compatible, free egress) -> public URL
  2. POST /{ig_user_id}/media  (media_type=REELS, video_url, caption)
  3. poll GET /{container_id}?fields=status_code until FINISHED
  4. POST /{ig_user_id}/media_publish (creation_id=container_id)
  5. delete the temp object from R2
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import boto3
import requests
from botocore.config import Config as BotoConfig

GRAPH = "https://graph.facebook.com"


def _r2_client(endpoint: str, access_key: str, secret_key: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=BotoConfig(signature_version="s3v4", region_name="auto"),
    )


def publish_reel(
    video_path: Path,
    caption: str,
    hashtags: list[str],
    ig_user_id: str,
    access_token: str,
    graph_version: str,
    r2_endpoint: str,
    r2_bucket: str,
    r2_access_key: str,
    r2_secret_key: str,
    r2_public_base: str,
    poll_timeout_s: int = 300,
) -> dict:
    if not all([ig_user_id, access_token, r2_endpoint, r2_bucket, r2_public_base]):
        raise RuntimeError("Instagram/R2 not configured")

    s3 = _r2_client(r2_endpoint, r2_access_key, r2_secret_key)
    key = f"reelforge/{uuid.uuid4().hex}.mp4"
    s3.upload_file(str(video_path), r2_bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    public_url = f"{r2_public_base.rstrip('/')}/{key}"

    try:
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
    finally:
        try:
            s3.delete_object(Bucket=r2_bucket, Key=key)
        except Exception:
            pass
