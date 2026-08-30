"""Telegram bridge — plain Bot API over `requests`.

Used by the cloud (GitHub Actions) path: send a clip with Approve/Reject buttons,
then edit that message later when the decision comes back through the Cloudflare
Worker. Also used by the local CLI path (`request_approval`, which polls).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

API = "https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, **kwargs):
    r = requests.post(API.format(token=token, method=method), timeout=120, **kwargs)
    r.raise_for_status()
    return r.json()["result"]


def send_message(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        return
    _call(
        token,
        "sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
    )


def send_clip_for_approval(
    token: str,
    chat_id: str,
    video_path: Path,
    caption: str,
    video_id: str,
) -> int:
    """Send the rendered clip with Approve / Reject buttons. Returns the message id.

    callback_data is ``ok:<yt_video_id>`` / ``no:<yt_video_id>`` — the Cloudflare
    Worker parses that and fires the matching GitHub Actions dispatch.
    """
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"ok:{video_id}"},
            {"text": "❌ Reject", "callback_data": f"no:{video_id}"},
        ]]
    }
    with open(video_path, "rb") as fh:
        res = _call(
            token,
            "sendVideo",
            data={
                "chat_id": chat_id,
                "caption": caption[:1000],
                "reply_markup": json.dumps(markup),
            },
            files={"video": fh},
        )
    return int(res["message_id"])


def edit_caption(token: str, chat_id: str, message_id: int, caption: str) -> None:
    if not token or not chat_id:
        return
    try:
        _call(
            token,
            "editMessageCaption",
            data={
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": caption[:1000],
            },
        )
    except Exception:
        # message may be too old to edit — fall back to a new message
        send_message(token, chat_id, caption)


# --- local CLI path only: blocking poll for a decision --------------------
def request_approval(
    token: str,
    chat_id: str,
    clip_id: int,
    video_path: Path,
    caption: str,
    timeout_s: int = 1800,
) -> bool:
    if not token or not chat_id:
        return False

    updates = _call(token, "getUpdates", data={"timeout": 0})
    offset = (updates[-1]["update_id"] + 1) if updates else 0

    markup = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"ok:{clip_id}"},
            {"text": "❌ Reject", "callback_data": f"no:{clip_id}"},
        ]]
    }
    with open(video_path, "rb") as fh:
        _call(
            token,
            "sendVideo",
            data={
                "chat_id": chat_id,
                "caption": f"Approve this clip?\n\n{caption[:900]}",
                "reply_markup": json.dumps(markup),
            },
            files={"video": fh},
        )

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        results = _call(token, "getUpdates", data={"offset": offset, "timeout": 30})
        for upd in results:
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if not cq:
                continue
            data = cq.get("data", "")
            if data.endswith(f":{clip_id}"):
                _call(
                    token,
                    "answerCallbackQuery",
                    data={"callback_query_id": cq["id"]},
                )
                return data.startswith("ok:")
    return False
