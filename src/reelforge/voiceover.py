"""Optional AI commentary voiceover — the transformative layer.

Turns a clip's ``commentary_script`` into a spoken wav using pyttsx3 (offline, free).
Swap ``synthesize`` for a cloud TTS SDK if you want a better voice; the signature
(text, out_path) -> Path is all render.py depends on.
"""

from __future__ import annotations

from pathlib import Path


def synthesize(text: str, out_path: Path) -> Path:
    import pyttsx3

    engine = pyttsx3.init()
    engine.setProperty("rate", 178)
    engine.save_to_file(text, str(out_path))
    engine.runAndWait()
    if not out_path.exists():
        raise RuntimeError("pyttsx3 produced no audio file")
    return out_path
