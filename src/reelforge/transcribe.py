"""Stage 2: transcribe with faster-whisper, keeping word-level timestamps.

Writes ``transcript.json`` in the job dir:
  {
    "language": "en",
    "duration": 3600.0,
    "segments": [{"start": .., "end": .., "text": ".."}],
    "words":    [{"start": .., "end": .., "word": ".."}]
  }

The word list drives caption timing in render.py; the segment list is what the
highlight model reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from faster_whisper import WhisperModel


def transcribe(video_path: Path, job_dir: Path, whisper_cfg: dict[str, Any]) -> dict:
    out = job_dir / "transcript.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))

    model = WhisperModel(
        whisper_cfg.get("model", "small"),
        device="auto",
        compute_type=whisper_cfg.get("compute_type", "int8"),
    )

    segments_iter, info = model.transcribe(
        str(video_path),
        language=whisper_cfg.get("language") or None,
        word_timestamps=True,
        vad_filter=True,
    )

    segments: list[dict] = []
    words: list[dict] = []
    for seg in segments_iter:
        segments.append(
            {"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip()}
        )
        for w in seg.words or []:
            words.append(
                {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word}
            )

    result = {
        "language": info.language,
        "duration": round(info.duration, 3),
        "segments": segments,
        "words": words,
    }
    out.write_text(json.dumps(result), encoding="utf-8")
    return result
