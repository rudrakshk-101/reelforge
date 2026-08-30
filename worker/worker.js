/**
 * reelforge Telegram <-> GitHub Actions bridge (Cloudflare Worker).
 *
 * Telegram webhook -> this Worker -> GitHub repository_dispatch -> Actions.
 *
 * Secrets (wrangler secret put ...):
 *   BOT_TOKEN   Telegram bot token
 *   CHAT_ID     your Telegram chat id (only this chat is allowed)
 *   TG_SECRET   random string, also passed to Telegram setWebhook secret_token
 *   GH_OWNER    e.g. rudrakshk-101
 *   GH_REPO     e.g. reelforge
 *   GH_PAT      fine-grained PAT, repo = GH_REPO, permission Contents: read+write
 */

const URL_RE = /(https?:\/\/[^\s]+)/g;

async function tg(env, method, body) {
  return fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function dispatch(env, event_type, client_payload) {
  const r = await fetch(
    `https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GH_PAT}`,
        accept: "application/vnd.github+json",
        "user-agent": "reelforge-worker",
        "content-type": "application/json",
      },
      body: JSON.stringify({ event_type, client_payload }),
    },
  );
  if (!r.ok) throw new Error(`dispatch ${event_type} -> ${r.status} ${await r.text()}`);
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok");
    if (request.headers.get("x-telegram-bot-api-secret-token") !== env.TG_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }

    try {
      // --- button taps ------------------------------------------------
      if (update.callback_query) {
        const cq = update.callback_query;
        const chatId = String(cq.message?.chat?.id ?? "");
        if (chatId !== String(env.CHAT_ID)) {
          await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
          return new Response("ok");
        }
        const [tag, videoId] = String(cq.data || "").split(":");
        const action = tag === "ok" ? "approve" : tag === "no" ? "reject" : null;
        if (action && videoId) {
          await dispatch(env, action, {
            video_id: videoId,
            chat_id: chatId,
            message_id: cq.message.message_id,
          });
          await tg(env, "answerCallbackQuery", {
            callback_query_id: cq.id,
            text: action === "approve" ? "Publishing…" : "Removing…",
          });
        } else {
          await tg(env, "answerCallbackQuery", { callback_query_id: cq.id });
        }
        return new Response("ok");
      }

      // --- text messages -------------------------------------------------
      const msg = update.message || update.channel_post;
      if (msg && String(msg.chat?.id) === String(env.CHAT_ID)) {
        const text = msg.text || "";
        if (text.startsWith("/start")) {
          await tg(env, "sendMessage", {
            chat_id: env.CHAT_ID,
            text: "Send me a YouTube link and I'll cut it into Shorts. You'll get each clip here to Approve or Reject.",
          });
          return new Response("ok");
        }
        const urls = text.match(URL_RE) || [];
        if (urls.length === 0) {
          await tg(env, "sendMessage", {
            chat_id: env.CHAT_ID,
            text: "Send me a YouTube link.",
          });
          return new Response("ok");
        }
        for (const url of urls) {
          await dispatch(env, "process", { url });
        }
        await tg(env, "sendMessage", {
          chat_id: env.CHAT_ID,
          text: `Queued ${urls.length} 🎬 — this takes a few minutes.`,
        });
      }
    } catch (err) {
      // surface the error to Telegram so failures aren't silent
      try {
        await tg(env, "sendMessage", {
          chat_id: env.CHAT_ID,
          text: `⚠️ bridge error: ${err.message}`.slice(0, 3500),
        });
      } catch {}
    }

    return new Response("ok");
  },
};
