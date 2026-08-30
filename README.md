# reelforge

Feed it a long video (YouTube link or file). It downloads, transcribes, lets Claude
pick the best 60-90s moments, cuts them to vertical with captions + a hook intro + an
AI commentary voiceover (the transformative layer), and publishes them as **YouTube
Shorts** and **Instagram Reels** — on a schedule, with an optional one-tap approval on
Telegram.

Your recurring effort: paste a URL into `sources.txt` (or set watched channels once
and do nothing), and optionally tap Approve on your phone.

## How it works

```
sources.txt / channels
        │
   ingest (yt-dlp)  →  transcribe (faster-whisper)  →  highlight (Gemini)
        │                                                    │
        │                                             clips.json
        ▼                                                    ▼
   render (ffmpeg: 9:16, captions, intro, voiceover, optional webcam PiP)
        │
   publish_mode:
     auto      → post immediately
     telegram  → send to phone, post on ✅ tap
     off       → render only
        │
   YouTube Data API v3   +   Instagram Graph API (via public GCS URL)
```

State + dedupe live in `reelforge.db` (SQLite). `daily_post_cap` in `config.yaml`
caps real posts per 24h per platform.

## Setup

1. `pip install -r requirements.txt && pip install -e .`, install `ffmpeg`.
2. `cp .env.example .env` and fill in — see the one-time API setup below.
3. Tune `config.yaml` (clips per video, publish_mode, caps, watched channels).

### One-time API setup (unavoidable, ~an hour)

| Service | What you need |
|---|---|
| **Gemini** | Free API key from https://aistudio.google.com/apikey → `GEMINI_API_KEY` |
| **YouTube** | GCP project, YouTube Data API v3 enabled, OAuth **Desktop** client json → `YOUTUBE_CLIENT_SECRETS_FILE`. First run opens a browser once. |
| **Instagram** | IG **Business/Creator** account linked to a Facebook Page; Meta app with `instagram_content_publish`; long-lived token → `IG_USER_ID`, `IG_ACCESS_TOKEN` |
| **GCS** | A bucket with public read (IG needs a public video URL) → `GCS_BUCKET`, service-account json → `GOOGLE_APPLICATION_CREDENTIALS` |
| **Telegram** | Create a bot with @BotFather, send it one message, get your chat id → `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

## Usage

```bash
reelforge add "https://www.youtube.com/watch?v=XXXX"
reelforge run          # process queued jobs
reelforge watch        # pick up sources.txt + channels, then run
reelforge status       # jobs + clips + 24h post counts
reelforge retry 3      # re-queue failed job 3
```

Schedule `reelforge watch` every few hours — see
[deploy/windows-task-scheduler.md](deploy/windows-task-scheduler.md) for local, or
[deploy/cloudbuild.yaml](deploy/cloudbuild.yaml) for a GCP Cloud Run Job.

## Copyright reality

Adding a reaction/commentary track reduces risk but does **not** make reposting movie
or third-party footage automatically legal. YouTube Content ID matches the original
audio/video regardless of overlays; expect occasional claims or blocks. The pipeline
mitigates by keeping clips short, capping `max_original_seconds`, and forcing an added
commentary layer — but the decision to publish is yours.

## Layout

```
src/reelforge/
  config.py  ingest.py  transcribe.py  highlight.py  render.py  voiceover.py
  publish_youtube.py  publish_instagram.py  notify.py  store.py  pipeline.py  cli.py
deploy/  Dockerfile  cloudbuild.yaml  windows-task-scheduler.md
```
