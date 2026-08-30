"""Publish a rendered clip as a YouTube Short via the Data API v3.

First run opens a browser for OAuth consent and caches the token to
``YOUTUBE_TOKEN_FILE``. Subsequent runs are non-interactive until the refresh
token expires.

Quota note: an upload costs ~1600 units of the default 10,000/day, so ~6 uploads/day.
"""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# force-ssl covers upload + update + delete (so "Reject" can remove an uploaded video)
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def _client(client_secrets: Path, token_file: Path):
    creds: Credentials | None = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def upload_short(
    video_path: Path,
    title: str,
    description: str,
    hashtags: list[str],
    client_secrets: Path,
    token_file: Path,
    privacy_status: str = "unlisted",
    category_id: str = "24",
    made_for_kids: bool = False,
) -> dict:
    yt = _client(client_secrets, token_file)

    tag_text = " ".join(f"#{h.lstrip('#')}" for h in hashtags)
    full_title = f"{title} #Shorts".strip()[:100]
    full_desc = f"{description}\n\n{tag_text} #Shorts".strip()

    body = {
        "snippet": {
            "title": full_title,
            "description": full_desc,
            "tags": [h.lstrip("#") for h in hashtags][:15],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }

    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        _, response = request.next_chunk()

    vid = response["id"]
    return {"video_id": vid, "url": f"https://youtube.com/shorts/{vid}"}


def delete_video(video_id: str, client_secrets: Path, token_file: Path) -> None:
    yt = _client(client_secrets, token_file)
    yt.videos().delete(id=video_id).execute()
