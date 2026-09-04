"""Stage 4: render one vertical clip per highlight with ffmpeg.

Pipeline per clip (single ffmpeg pass with a filter_complex):
  1. cut [start, end], capped to max_original_seconds
  2. 1080x1920 canvas: blurred, scaled-up copy as background + fitted foreground
  3. burned-in word-timed captions (.ass generated from transcript words)
  4. hook_title text overlaid for the first `intro_card_seconds`
  5. optional AI voiceover mixed over ducked original audio
  6. optional webcam reaction video composited as picture-in-picture

Requires `ffmpeg` and `ffprobe` on PATH.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import voiceover

# Windows ffmpeg (gyan build) has no default fontconfig config, and a bare Linux
# runner (GitHub Actions) has no fonts installed at all — both crash drawtext/libass
# without a real font file. Try known bold-sans fonts across platforms; whichever
# exists first wins, and its family name is what the ASS caption style uses.
_FONT_CANDIDATES: list[tuple[str, Path]] = [
    ("DejaVu Sans", Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
    ("Liberation Sans", Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")),
    ("Noto Sans", Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf")),
    ("Arial", Path(r"C:\Windows\Fonts\arialbd.ttf")),
    ("Segoe UI", Path(r"C:\Windows\Fonts\segoeuib.ttf")),
    ("Nirmala UI", Path(r"C:\Windows\Fonts\Nirmala.ttc")),
]
_TITLE_FONT_FAMILY, _TITLE_FONT = next(
    ((fam, p) for fam, p in _FONT_CANDIDATES if p.exists()), ("Sans", None)
)
# Every directory from the candidates above that actually exists on this host —
# handed to both fontconfig and the ass filter's fontsdir so family-name lookups work.
_FONT_DIRS = sorted({p.parent for _, p in _FONT_CANDIDATES if p.exists()})


def _ff_path(p: Path | str) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    return str(p).replace("\\", "/").replace(":", r"\:")


def _fontconfig_file(job_dir: Path) -> Path:
    conf = job_dir / "fonts.conf"
    if not conf.exists():
        dirs_xml = "".join(f"  <dir>{d.as_posix()}</dir>\n" for d in _FONT_DIRS)
        conf.write_text(
            '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
            "<fontconfig>\n" + dirs_xml + "  <cachedir>~/.cache/fontconfig</cachedir>\n"
            '  <include ignore_missing="yes">conf.d</include>\n</fontconfig>\n',
            encoding="utf-8",
        )
    return conf


@dataclass
class RenderedClip:
    idx: int
    path: Path
    sidecar_path: Path


def _run(cmd: list[str], fontconfig: Path | None = None) -> None:
    env = os.environ.copy()
    if fontconfig is not None:
        env["FONTCONFIG_FILE"] = str(fontconfig)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed ({proc.returncode})\ncmd: {shlex.join(cmd)}\n{proc.stderr[-4000:]}"
        )


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def _write_ass(words: list[dict], clip_start: float, clip_end: float, out: Path) -> None:
    """Build a simple 2-3 word rolling caption track for the clip window."""
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        f"Style: Cap,{_TITLE_FONT_FAMILY},64,&H00FFFFFF,&H00000000,&H80000000,-1,1,4,1,2,60,60,320\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def ts(t: float) -> str:
        t = max(0.0, t)
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h:d}:{m:02d}:{s:05.2f}"

    in_window = [w for w in words if w["end"] > clip_start and w["start"] < clip_end]
    lines = []
    group: list[dict] = []
    for w in in_window:
        group.append(w)
        if len(group) >= 3 or w["word"].strip().endswith((".", "!", "?", ",")):
            g_start = group[0]["start"] - clip_start
            g_end = group[-1]["end"] - clip_start
            text = _ass_escape("".join(g["word"] for g in group).strip())
            lines.append(f"Dialogue: 0,{ts(g_start)},{ts(g_end)},Cap,,0,0,0,,{text}")
            group = []
    if group:
        g_start = group[0]["start"] - clip_start
        g_end = group[-1]["end"] - clip_start
        text = _ass_escape("".join(g["word"] for g in group).strip())
        lines.append(f"Dialogue: 0,{ts(g_start)},{ts(g_end)},Cap,,0,0,0,,{text}")

    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def _dt_sanitize(text: str) -> str:
    """Strip characters that break drawtext's mini-language."""
    out = []
    for ch in text:
        if ch.isalnum() or ch in " !?-&/'":
            out.append(ch)
    return " ".join("".join(out).split()).replace("'", "’").replace(":", "-")


def _overlay_filters(texts: list[str], clip_dur: float) -> list[str]:
    """One big centered caption after another across the whole clip duration."""
    texts = [t for t in (s.strip() for s in texts) if t][:5]
    if not texts:
        return []
    fontfile = f"fontfile='{_ff_path(_TITLE_FONT)}':" if _TITLE_FONT else ""
    slot = clip_dur / len(texts)
    filters = []
    for i, raw in enumerate(texts):
        t0 = round(i * slot, 2)
        t1 = round(clip_dur if i == len(texts) - 1 else (i + 1) * slot, 2)
        txt = _dt_sanitize(raw)
        # First phrase (the hook) sits a touch higher and larger.
        size = 74 if i == 0 else 66
        filters.append(
            f"drawtext={fontfile}text='{txt}':fontcolor=white:fontsize={size}:"
            f"box=1:boxcolor=black@0.55:boxborderw=28:line_spacing=8:"
            f"x=(w-text_w)/2:y=h*0.12:enable='between(t,{t0},{t1})'"
        )
    return filters


def render_clip(
    idx: int,
    clip: dict,
    source_video: Path,
    words: list[dict],
    job_dir: Path,
    render_cfg: dict[str, Any],
    max_original_seconds: float,
) -> RenderedClip:
    W = int(render_cfg.get("width", 1080))
    H = int(render_cfg.get("height", 1920))
    start = float(clip["start"])
    end = float(clip["end"])
    if end - start > max_original_seconds:
        end = start + max_original_seconds
    dur = round(end - start, 3)

    clips_dir = job_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    out_path = clips_dir / f"clip_{idx:02d}.mp4"
    sidecar_path = clips_dir / f"clip_{idx:02d}.json"

    ass_path = clips_dir / f"clip_{idx:02d}.ass"
    if render_cfg.get("captions", True):
        _write_ass(words, start, end, ass_path)

    # --- build filter_complex --------------------------------------------
    # [0:v] is the trimmed source. Background = blurred cover; foreground = contain.
    bg = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=40:2[bg]"
    fg = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease[fg]"
    chain = [bg, fg, "[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]"]
    last = "[v0]"

    inputs = ["-ss", f"{start}", "-t", f"{dur}", "-i", str(source_video)]
    extra_input_idx = 1

    # webcam PiP
    webcam = render_cfg.get("webcam_overlay_path") or ""
    if webcam and Path(webcam).exists():
        inputs += ["-stream_loop", "-1", "-t", f"{dur}", "-i", webcam]
        cam_idx = extra_input_idx
        extra_input_idx += 1
        chain.append(
            f"[{cam_idx}:v]scale={W//3}:-1,setsar=1[cam]"
        )
        chain.append(f"{last}[cam]overlay=W-w-40:40[v1]")
        last = "[v1]"

    # captions
    if render_cfg.get("captions", True):
        chain.append(
            f"{last}ass='{_ff_path(ass_path)}':fontsdir='{_ff_path(_FONT_DIRS[0])}'[v2]"
            if _FONT_DIRS
            else f"{last}ass='{_ff_path(ass_path)}'[v2]"
        )
        last = "[v2]"

    # big on-screen text callouts across the whole clip
    overlay_texts = []
    if render_cfg.get("on_screen_text", True):
        overlay_texts = clip.get("on_screen_text") or (
            [clip["hook_title"]] if clip.get("hook_title") else []
        )
    for i, filt in enumerate(_overlay_filters(overlay_texts, dur)):
        chain.append(f"{last}{filt}[vt{i}]")
        last = f"[vt{i}]"
    chain.append(f"{last}null[vout]")
    last = "[vout]"

    # --- audio ----------------------------------------------------------
    # Keep the original audio. Optionally mix a background music bed under it.
    # Optional AI voiceover (off by default) still supported.
    vo_path = clips_dir / f"clip_{idx:02d}_vo.wav"
    use_vo = bool(render_cfg.get("voiceover", False)) and clip.get("commentary_script")
    music = render_cfg.get("background_music_path") or ""
    have_music = bool(music) and Path(music).exists()

    mix_parts = ["[0:a]aresample=44100,volume=1.0[a0]"]
    mix_inputs = ["[a0]"]
    if use_vo:
        voiceover.synthesize(clip["commentary_script"], vo_path)
        inputs += ["-i", str(vo_path)]
        vo_idx = extra_input_idx
        extra_input_idx += 1
        mix_parts.append(f"[{vo_idx}:a]aresample=44100,volume=1.2[avo]")
        mix_inputs.append("[avo]")
        mix_parts[0] = "[0:a]aresample=44100,volume=0.4[a0]"
    if have_music:
        inputs += ["-stream_loop", "-1", "-t", f"{dur}", "-i", music]
        mus_idx = extra_input_idx
        extra_input_idx += 1
        mix_parts.append(f"[{mus_idx}:a]aresample=44100,volume=0.12[amus]")
        mix_inputs.append("[amus]")

    if len(mix_inputs) > 1:
        chain.extend(mix_parts)
        chain.append(
            f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first:"
            f"dropout_transition=0:normalize=0[aout]"
        )
        audio_map = ["-map", "[aout]"]
    else:
        audio_map = ["-map", "0:a?"]

    filter_complex = ";".join(chain)

    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        filter_complex,
        "-map",
        last,
        *audio_map,
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    _run(cmd, fontconfig=_fontconfig_file(job_dir))

    hashtags = clip.get("hashtags", [])
    if isinstance(hashtags, str):
        hashtags = [hashtags]
    sidecar = {
        "idx": idx,
        "hook_title": clip.get("hook_title", ""),
        "caption": clip.get("caption", ""),
        "hashtags": hashtags,
        "start": start,
        "end": end,
        "commentary_script": clip.get("commentary_script", ""),
    }
    sidecar_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

    return RenderedClip(idx=idx, path=out_path, sidecar_path=sidecar_path)
