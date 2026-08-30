# reelforge on GitHub Actions (serverless, $0)

No always-on server. A Cloudflare Worker turns Telegram events into
`repository_dispatch` calls; GitHub Actions runs the pipeline.

```
Telegram  ──►  Cloudflare Worker  ──►  GitHub repository_dispatch
                                            │
                            ┌───────────────┴───────────────┐
                        process.yml                     decide.yml
                  download→whisper→gemini→render     approve → make public
                  upload UNLISTED → send to phone    reject  → delete
```

## 1. GitHub repository secrets

`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | Value |
|---|---|
| `GEMINI_API_KEY` | from https://aistudio.google.com/apikey |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TELEGRAM_CHAT_ID` | your numeric chat id |
| `YOUTUBE_CLIENT_SECRET` | full contents of `secrets/youtube_client_secret.json` |
| `YOUTUBE_TOKEN` | full contents of `secrets/youtube_token.json` (run `reelforge auth-youtube` first) |
| `R2_ENDPOINT` `R2_BUCKET` `R2_ACCESS_KEY` `R2_SECRET_KEY` `R2_PUBLIC_BASE` | Cloudflare R2 (only if using Instagram) |
| `IG_USER_ID` `IG_ACCESS_TOKEN` | Instagram Graph API (only if using Instagram) |

Get the YouTube token JSON:

```bash
cd reelforge
.\.venv\Scripts\python -m reelforge auth-youtube      # opens a browser once
gh secret set YOUTUBE_TOKEN < secrets/youtube_token.json
gh secret set YOUTUBE_CLIENT_SECRET < secrets/youtube_client_secret.json
gh secret set GEMINI_API_KEY --body "..."
gh secret set TELEGRAM_BOT_TOKEN --body "..."
gh secret set TELEGRAM_CHAT_ID --body "..."
```

## 2. Cloudflare Worker

Free Cloudflare account (no card). Then:

```bash
npm install -g wrangler
cd worker
wrangler login
wrangler secret put BOT_TOKEN     # Telegram bot token
wrangler secret put CHAT_ID       # your chat id
wrangler secret put TG_SECRET     # any random string, e.g. `openssl rand -hex 16`
wrangler secret put GH_OWNER      # rudrakshk-101
wrangler secret put GH_REPO       # reelforge
wrangler secret put GH_PAT        # fine-grained PAT, see below
wrangler deploy
```

`wrangler deploy` prints the URL, e.g. `https://reelforge-bridge.<you>.workers.dev`.

**GH_PAT** — GitHub → Settings → Developer settings → Fine-grained tokens → Generate:
- Repository access: only `reelforge`
- Permissions: **Contents → Read and write** (this is what `dispatches` needs)
- Copy the `github_pat_...` value

## 3. Point Telegram at the Worker

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://reelforge-bridge.<you>.workers.dev&secret_token=<TG_SECRET>"
```

Should return `{"ok":true,"result":true,"description":"Webhook was set"}`.

## 4. Use it

Message the bot a YouTube link. Or trigger from the GitHub UI:
`Actions → process → Run workflow → paste URL`.

## Notes
- State (`reelforge.db`) is committed back to `main` with `[skip ci]` messages.
- One job runs at a time (`concurrency: reelforge`).
- `daily_post_cap` in `config.yaml` still applies.
- yt-dlp is reinstalled every run, so it never goes stale.
