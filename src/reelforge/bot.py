"""Long-running Telegram bot — the whole interface.

Send it a YouTube link -> it queues, processes, and sends each rendered clip back
with Approve / Reject buttons. Approve publishes; Reject deletes.

Runs one job at a time (a lock) so a 1 GB VM doesn't fall over transcribing two
videos at once. Started as a systemd service on the VM.
"""

from __future__ import annotations

import asyncio
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import pipeline
from .config import get_config
from .store import Store

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("reelforge.bot")

CFG = get_config()
URL_RE = re.compile(r"https?://[^\s]+")
_work_lock = asyncio.Lock()


def _authorized(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and str(chat.id) == str(CFG.secrets.telegram_chat_id)


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    await update.message.reply_text(
        "Send me a YouTube link and I'll cut it into Shorts. "
        "You'll get each clip here to Approve or Reject before it posts."
    )


async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    store = Store(CFG.db_file)
    try:
        rows = store._conn.execute(
            "SELECT id, status, title FROM jobs ORDER BY id DESC LIMIT 5"
        ).fetchall()
        yt = store.published_in_last_24h("youtube")
        ig = store.published_in_last_24h("instagram")
    finally:
        store.close()
    lines = [f"#{r['id']} {r['status']} — {(r['title'] or '')[:50]}" for r in rows]
    lines.append(f"\nposted last 24h — youtube: {yt}, instagram: {ig}")
    await update.message.reply_text("\n".join(lines) or "no jobs yet")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return
    urls = URL_RE.findall(update.message.text or "")
    if not urls:
        await update.message.reply_text("Send me a YouTube link.")
        return

    store = Store(CFG.db_file)
    try:
        new = [(store.add_job(u), u) for u in urls]
    finally:
        store.close()

    queued = [(jid, u) for jid, u in new if jid]
    dupes = len(new) - len(queued)
    msg = f"Queued {len(queued)} 🎬"
    if dupes:
        msg += f" ({dupes} already done)"
    if queued:
        msg += " — this takes a few minutes."
    await update.message.reply_text(msg)

    for jid, _u in queued:
        ctx.application.create_task(_process(jid, ctx))


async def _process(job_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = CFG.secrets.telegram_chat_id
    async with _work_lock:
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        loop = asyncio.get_running_loop()
        try:
            clip_ids = await loop.run_in_executor(None, pipeline.prepare_job, job_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed", job_id)
            await ctx.bot.send_message(chat_id, f"❌ Job {job_id} failed:\n{exc}")
            return

        store = Store(CFG.db_file)
        try:
            for cid in clip_ids:
                row = store.clip(cid)
                if row["status"] == "published":
                    continue
                kb = InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton("✅ Approve", callback_data=f"ok:{cid}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"no:{cid}"),
                    ]]
                )
                caption = f"{row['hook_title']}\n\n{row['caption']}\n{row['hashtags'] or ''}"
                with open(row["render_path"], "rb") as fh:
                    await ctx.bot.send_video(
                        chat_id, fh, caption=caption[:1000], reply_markup=kb
                    )
        finally:
            store.close()


async def on_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not _authorized(update):
        return
    try:
        action, cid_s = q.data.split(":")
        cid = int(cid_s)
    except ValueError:
        return

    store = Store(CFG.db_file)
    row = store.clip(cid)
    store.close()
    if row is None:
        await q.edit_message_caption(caption="(clip not found)")
        return

    loop = asyncio.get_running_loop()

    if action == "no":
        await q.edit_message_caption(caption=f"❌ Rejected — {row['hook_title']}")
        await loop.run_in_executor(None, pipeline.delete_clip_video, cid)
        return

    await q.edit_message_caption(caption=f"⏳ Posting — {row['hook_title']}")
    try:
        await loop.run_in_executor(None, pipeline.publish_clip, cid)
        store = Store(CFG.db_file)
        row = store.clip(cid)
        store.close()
        link = row["yt_url"] or row["ig_url"] or "(posted)"
        await q.edit_message_caption(caption=f"✅ Posted — {row['hook_title']}\n{link}")
    except Exception as exc:  # noqa: BLE001
        log.exception("publish clip %s failed", cid)
        await q.edit_message_caption(
            caption=f"⚠️ Post failed — {row['hook_title']}\n{exc}"
        )


def main() -> None:
    app = Application.builder().token(CFG.secrets.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("reelforge bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
