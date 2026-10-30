"""Slide timeline and chapters.

A slide change is a picture cut that holds: ffmpeg-skill's `scenes` finds the cuts (scdet), and cuts closer
than --min-dwell seconds are ignored (its --min-scene), so a slide must stay up at least that long to count.
Animated builds, video clips inside a slide and a camera cutting away still produce cuts; the timeline says
what was measured and the contact sheet (--sheet) is the way to check it.
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional

from . import __version__
from .engine import FfmpegSkill, Runner, run_tool, subprocess_runner

TIMELINE_SCHEMA = "slide-sync/timeline@1"
DEFAULT_THRESHOLD = 8.0
DEFAULT_MIN_DWELL = 2.0
YOUTUBE_MIN_CHAPTERS = 3
YOUTUBE_MIN_CHAPTER_S = 10.0


class SlideSyncError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _round(t: float) -> float:
    return round(float(t), 3)


def detect(skill: FfmpegSkill, video: str, *, threshold: float = DEFAULT_THRESHOLD, min_dwell: float = DEFAULT_MIN_DWELL,
           sheet: Optional[str] = None, timeout: float = 3600.0, runner: Runner = subprocess_runner) -> Dict[str, Any]:
    """Slide timeline of a recording: one entry per slide (start, end, duration) and the change times."""
    if not os.path.isfile(video):
        raise SlideSyncError("INPUT_NOT_FOUND", f"no such file: {video}")
    if not (0 < threshold <= 100):
        raise SlideSyncError("INVALID_ARGUMENT", "--threshold must be in (0, 100]")
    if min_dwell <= 0:
        raise SlideSyncError("INVALID_ARGUMENT", "--min-dwell must be above 0")
    args = [video, "--threshold", str(threshold), "--min-scene", str(min_dwell)]
    if sheet:
        args += ["--sheet", sheet]
    doc = run_tool(skill, "scenes", args, timeout=timeout, runner=runner)
    duration = doc.get("duration")
    scenes = doc.get("scenes")
    if not isinstance(duration, (int, float)) or not isinstance(scenes, list) or not scenes:
        raise SlideSyncError("ENGINE_FAILED", "ffmpeg-skill scenes returned no duration or no scenes")
    slides = []
    for i, s in enumerate(sorted(scenes, key=lambda s: float(s["start"]))):
        start, end = _round(s["start"]), _round(s["end"])
        slides.append({"index": i + 1, "start": start, "end": end, "duration": _round(end - start)})
    return {
        "schema": TIMELINE_SCHEMA,
        "source": {"path": os.path.abspath(video), "duration": _round(duration)},
        "detector": {"skill": "slide-sync", "skill_version": __version__, "engine": "ffmpeg-skill/scenes",
                     "engine_version": skill.version, "method": "scdet cuts held at least min_dwell seconds",
                     "threshold": threshold, "min_dwell": min_dwell},
        "slide_count": len(slides),
        "changes": [s["start"] for s in slides[1:]],
        "slides": slides,
        "sheet": doc.get("sheet"),
        "provenance": "OBSERVED",
    }


def read_titles(path: Optional[str]) -> List[str]:
    if not path:
        return []
    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


def timecode(t: float) -> str:
    total = int(round(t))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chapters(timeline: Dict[str, Any], *, titles: Optional[List[str]] = None, prefix: str = "Slide",
             min_chapter: float = 0.0) -> Dict[str, Any]:
    """Chapters from a slide timeline: one per slide, titled from `titles` in order (then `prefix N`). Slides
    shorter than `min_chapter` seconds are merged into the chapter before them."""
    if timeline.get("schema") != TIMELINE_SCHEMA:
        raise SlideSyncError("INVALID_TIMELINE", f"not a {TIMELINE_SCHEMA} document")
    slides = timeline.get("slides") or []
    if not slides:
        raise SlideSyncError("INVALID_TIMELINE", "the timeline has no slides")
    titles = titles or []
    out: List[Dict[str, Any]] = []
    for s in slides:
        title = titles[s["index"] - 1] if s["index"] - 1 < len(titles) else f"{prefix} {s['index']}"
        if out and s["duration"] < min_chapter:
            out[-1]["end"] = s["end"]
            out[-1]["slides"].append(s["index"])
            continue
        out.append({"start": s["start"], "end": s["end"], "title": title, "slides": [s["index"]]})
    out[0]["start"] = 0.0
    warnings = []
    if len(out) < YOUTUBE_MIN_CHAPTERS:
        warnings.append(f"YouTube shows chapters only with at least {YOUTUBE_MIN_CHAPTERS}; this has {len(out)}")
    short = [c["title"] for c in out if c["end"] - c["start"] < YOUTUBE_MIN_CHAPTER_S]
    if short:
        warnings.append(f"YouTube needs every chapter to be at least {YOUTUBE_MIN_CHAPTER_S:.0f}s; shorter: "
                        + ", ".join(short[:5]) + (" ..." if len(short) > 5 else "") + " (use --min-chapter 10)")
    if len(titles) and len(titles) != len(slides):
        warnings.append(f"{len(titles)} title(s) for {len(slides)} slide(s): titles are used in order")
    return {"chapters": out, "text": "".join(f"{timecode(c['start'])} {c['title']}\n" for c in out), "warnings": warnings}


def write_chapters(skill: FfmpegSkill, video: str, chapter_doc: Dict[str, Any], output: str, *, overwrite: bool = False,
                   timeout: float = 3600.0, runner: Runner = subprocess_runner) -> Dict[str, Any]:
    """A copy of `video` with the chapters as container metadata, through ffmpeg-skill `metadata` (streams are copied
    bit for bit). The input is never written."""
    if os.path.abspath(output) == os.path.abspath(video):
        raise SlideSyncError("OUTPUT_IS_INPUT", "-o must not be the input file")
    with tempfile.TemporaryDirectory(prefix="slide_sync_") as tmp:
        chapters_file = os.path.join(tmp, "chapters.txt")
        with open(chapters_file, "w", encoding="utf-8") as fh:
            for c in chapter_doc["chapters"]:
                fh.write(f"{c['start']:.3f} {c['title']}\n")
        args = [video, "--chapters", chapters_file, "-o", output] + (["--overwrite"] if overwrite else [])
        doc = run_tool(skill, "metadata", args, timeout=timeout, runner=runner)
    return {"output": doc.get("output") or output, "engine_status": doc.get("status"), "commands": doc.get("commands", [])}
