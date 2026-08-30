# Run reelforge automatically on Windows

## One-time

```powershell
cd C:\Users\Lenovo\Downloads\WST\reelforge
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\pip install -e .
copy .env.example .env   # then fill in .env
```

Install ffmpeg and make sure `ffmpeg -version` works in a new shell
(`winget install Gyan.FFmpeg` or `choco install ffmpeg`).

Do the first YouTube OAuth interactively so the browser consent can happen once:

```powershell
.\.venv\Scripts\python -m reelforge add "https://www.youtube.com/watch?v=XXXX"
.\.venv\Scripts\python -m reelforge run
```

## Schedule it

Create a scheduled task that runs every 3 hours:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\Users\Lenovo\Downloads\WST\reelforge\.venv\Scripts\python.exe" `
             -Argument "-m reelforge watch" `
             -WorkingDirectory "C:\Users\Lenovo\Downloads\WST\reelforge"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
             -RepetitionInterval (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName "reelforge" -Action $action -Trigger $trigger `
             -Description "Auto-clip + publish shorts/reels"
```

Logs land in `logs\reelforge_YYYYMMDD.log`. The `daily_post_cap` in `config.yaml`
prevents the 3-hourly runs from over-posting.

## Day-to-day

Paste URLs into `sources.txt` (one per line), or add channel URLs under
`watch.channels` in `config.yaml` to have new uploads picked up with no action at all.
With `publish_mode: telegram` you get each clip on your phone with Approve / Reject
buttons before anything goes live.
