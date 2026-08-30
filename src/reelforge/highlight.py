"""Stage 3: pick the best short clips with Gemini.

Feeds the transcript segments to the Gemini API and asks for the top N
self-contained, hook-forward moments. Uses structured output (response_schema) so
the reply is validated JSON rather than free text.

Output written to ``clips.json`` in the job dir:
  [{"start": .., "end": .., "hook_title": "..", "caption": "..",
    "hashtags": [".."], "commentary_script": ".."}]
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel


class Clip(BaseModel):
    start: float
    end: float
    hook_title: str
    caption: str
    hashtags: list[str]
    on_screen_text: list[str]
    commentary_script: str
    reason: str


class ClipList(BaseModel):
    clips: list[Clip]


SYSTEM = """You are a short-form video editor. You are given a transcript of a long
video with timestamps. Select the strongest self-contained moments to cut into
vertical Shorts/Reels.

Score candidates on: hook strength in the first 2 seconds, emotional or informational
payoff, whether it stands alone without context, and quotability.

For each clip return:
- start/end: seconds into the source. Duration MUST be between {min_s} and {max_s}
  seconds and should end on a natural sentence boundary.
- hook_title: <=60 chars, punchy, no clickbait lies.
- caption: 1-2 sentences for the post body.
- hashtags: 3-6 relevant tags, no leading '#'.
- on_screen_text: 3 to 5 SHORT punchy phrases (2-5 words each, Title Case, no end
  punctuation) that will be burned onto the video one after another across its
  duration as big captions. Make them scroll-stopping and specific to what is being
  shown, e.g. "New Matte Space Black", "Cherry Red Is Back", "Titanium Frame Stays".
  The first phrase is the hook - it must create curiosity in under 2 seconds.
- commentary_script: 2-4 sentences of ORIGINAL written commentary/context (used as the
  post description, not spoken). Add analysis or opinion, don't just narrate.
- reason: one line on why this moment works.

Return exactly {n} clips, non-overlapping, spread across the video."""


def _condense_segments(segments: list[dict], max_chars: int = 200000) -> str:
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments]
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n...\n" + text[-half:]


def select_highlights(
    transcript: dict,
    job_dir: Path,
    source_title: str,
    llm_cfg: dict[str, Any],
    n: int,
    min_s: float,
    max_s: float,
    api_key: str,
) -> list[dict]:
    out = job_dir / "clips.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))

    client = genai.Client(api_key=api_key)
    system = SYSTEM.format(n=n, min_s=min_s, max_s=max_s)
    user = (
        f"Source video title: {source_title}\n"
        f"Total duration: {transcript.get('duration', 0):.0f}s\n\n"
        f"Transcript:\n{_condense_segments(transcript['segments'])}"
    )

    cfg_obj = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=ClipList,
        temperature=0.7,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    # Try the configured model first, then fall through known-good alternates
    # (Gemini rotates model availability and 503s under load).
    models = [llm_cfg.get("model", "gemini-3.6-flash")] + [
        m
        for m in ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite")
        if m != llm_cfg.get("model", "gemini-3.6-flash")
    ]

    response = None
    last_err: Exception | None = None
    for model in models:
        for attempt in range(4):
            try:
                response = client.models.generate_content(
                    model=model, contents=user, config=cfg_obj
                )
                break
            except genai_errors.ServerError as exc:  # 5xx incl. 503 overloaded
                last_err = exc
                time.sleep(3 * (attempt + 1))
            except genai_errors.ClientError as exc:  # 404 model gone, 429 quota
                last_err = exc
                if getattr(exc, "code", None) == 429 and attempt < 3:
                    time.sleep(15 * (attempt + 1))
                    continue
                break  # move to next model
        if response is not None:
            break
    if response is None:
        raise RuntimeError(f"all Gemini models failed; last error: {last_err}")

    parsed: ClipList = response.parsed  # type: ignore[assignment]
    clips = [c.model_dump() for c in parsed.clips]

    # Clamp durations to the configured window as a safety net.
    for c in clips:
        c["start"] = max(0.0, float(c["start"]))
        c["end"] = float(c["end"])
        dur = c["end"] - c["start"]
        if dur > max_s:
            c["end"] = c["start"] + max_s
        if dur < min_s:
            c["end"] = c["start"] + min_s

    out.write_text(json.dumps(clips, indent=2), encoding="utf-8")
    return clips
