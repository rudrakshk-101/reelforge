"""Telegram bridge: notifications + approve/reject gate.

Kept deliberately simple — plain Bot API over `requests`, no long-running process.
`request_approval` sends the rendered clip with inline Approve/Reject buttons and
long-polls `getUpdates` until the user taps one (or a timeout is hit).

For this to work the bot must have received at least one message from you (so it can
DM you) and `TELEGRAM_CHAT_ID` must be your chat id.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

API = "https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, **kwargs):
    r = requests.post(API.format(token=token, method=method), timeout=60, **kwargs)
    r.raise_for_status()
    return r.json()["result"]


def send_message(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        return
    _call(token, "sendMessage", data={"chat_id": chat_id, "text": text, "disable_web_page_preview": False})


def send_clip(token: str, chat_id: str, video_path: Path, caption: str) -> None:
    if not token or not chat_id:
        return
    with open(video_path, "rb") as fh:
        _call(
            token,
            "sendVideo",
            data={"chat_id": chat_id, "caption": caption[:1000]},
            files={"video": fh},
        )


def request_approval(
    token: str,
    chat_id: str,
    clip_id: int,
    video_path: Path,
    caption: str,
    timeout_s: int = 1800,
) -> bool:
    """Returns True if approved, False if rejected or timed out."""
    if not token or not chat_id:
        # No Telegram configured -> fail safe: do not publish.
        return False

    # Clear any backlog so we only see fresh taps.
    updates = _call(token, "getUpdates", data={"timeout": 0})
    offset = (updates[-1]["update_id"] + 1) if updates else 0

    with open(video_path, "rb") as fh:
        _call(
            token,
            "sendVideo",
            data={
                "chat_id": chat_id,
                "caption": f"Approve this clip?\n\n{caption[:900]}",
                "reply_markup": (
                    '{"inline_keyboard":[[{"text":"✅ Approve","callback_data":"ok:%d"},'
                    '{"text":"❌ Reject","callback_data":"no:%d"}]]}' % (clip_id, clip_id)
                ),
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
                _call(token, "answerCallbackQuery", data={"callback_query_id": cq["id"]})
                return data.startswith("ok:")
    return False
