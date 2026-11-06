"""CLEAN: the deck page, sharp, over the captured slide where `match` is confident (`slide-sync/clean@1`).

A camera on a projector gives a soft, keystoned, washed-out slide; `match` already says which deck page it is, how
sure it is, and has located and straightened the slide to compare it. Where the match is confident, the frames of that
time show the deck page instead, rendered from the imported page image at the recording's frame size (page aspect
kept, padded); everywhere else the capture stays as it was. The media work is ffmpeg-skill's (docs/decisions.md
ADR-006):

1. Each replaced page becomes a still clip at the output size (`insert --width --height --fps`).
2. One `broll` puts each clip over its span of the recording (`--at --end`, the recording's audio kept, `--audio a`).

Every segment says why it was replaced or kept (`confident_match`, `low_confidence`, `mixed`, `unmatched`).
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .core import SlideSyncError
from .engine import EngineError, FfmpegSkill, Runner, subprocess_runner
from .layout import _probe, _r
from .match import MATCH_SCHEMA, load_deck

CLEAN_SCHEMA = "slide-sync/clean@1"
CLEAN_TOOLS = ("probe", "insert", "broll")
DEFAULT_MIN_CONFIDENCE = 0.8   # ADR-006: above match's 0.7; every right page in ADR-002's fixtures scored 0.86 or more
CLIP_SLACK = 0.1               # seconds: a page clip is this much longer than its longest span (broll trims it)

RULES = {
    "confident_match": "a deck page matched with at least --min-confidence: the page replaces the capture",
    "low_confidence": "a deck page matched, but below --min-confidence: the capture is kept",
    "mixed": "the segment's frames showed different pages: the capture is kept",
    "unmatched": "no deck page matched: the capture is kept",
}


def page_size(page_aspect: float, width: int, height: int) -> Tuple[int, int]:
    """The page fitted inside the output frame, aspect kept, even sides (4:2:0)."""
    if page_aspect >= width / height:
        w, h = width, width / page_aspect
    else:
        w, h = height * page_aspect, height
    return max(2, int(round(w / 2)) * 2), max(2, int(round(h / 2)) * 2)


def decide_segments(segments: Sequence[Dict[str, Any]], *, min_confidence: float) -> List[Dict[str, Any]]:
    """Replace or keep, with the rule, for every segment of a match. Pure."""
    out = []
    for s in segments:
        page, conf = s.get("page"), s.get("confidence")
        if page is None:
            rule = "unmatched"
        elif s.get("mixed"):
            rule = "mixed"
        elif not isinstance(conf, (int, float)) or conf < min_confidence:
            rule = "low_confidence"
        else:
            rule = "confident_match"
        out.append({"segment": s.get("index"), "start": _r(s["start"]), "end": _r(s["end"]), "page": page,
                    "confidence": conf, "view": s.get("view"),
                    "action": "replace" if rule == "confident_match" else "keep", "rule": rule, "why": RULES[rule]})
    return out


def replaced_spans(decisions: Sequence[Dict[str, Any]], duration: Optional[float] = None) -> List[Dict[str, Any]]:
    """Consecutive replaced segments of the same page as one span, clipped to the recording's length."""
    spans: List[Dict[str, Any]] = []
    for d in decisions:
        if d["action"] != "replace":
            continue
        end = min(d["end"], duration) if duration is not None else d["end"]
        if end <= d["start"]:
            continue
        if spans and spans[-1]["page"] == d["page"] and abs(spans[-1]["end"] - d["start"]) < 1e-6:
            spans[-1]["end"] = _r(end)
            spans[-1]["segments"].append(d["segment"])
        else:
            spans.append({"page": d["page"], "start": d["start"], "end": _r(end), "segments": [d["segment"]]})
    for s in spans:
        s["duration"] = _r(s["end"] - s["start"])
    return spans


def plan_clean(skill: FfmpegSkill, video: str, match_doc: Dict[str, Any], *, deck_dir: Optional[str] = None,
               min_confidence: float = DEFAULT_MIN_CONFIDENCE, pad_color: str = "black", output: Optional[str] = None,
               match_path: Optional[str] = None, timeout: float = 3600.0,
               runner: Runner = subprocess_runner) -> Dict[str, Any]:
    """The clean-pages plan: replace or keep (and why) for every segment, the replaced spans, and the ffmpeg-skill calls
    (`insert` per page, then one `broll`). Measures the recording with ffmpeg-skill `probe`; writes nothing."""
    if not os.path.isfile(video):
        raise SlideSyncError("INPUT_NOT_FOUND", f"no such file: {video}")
    if not isinstance(match_doc, dict) or match_doc.get("schema") != MATCH_SCHEMA or not match_doc.get("segments"):
        raise SlideSyncError("INVALID_ARGUMENT", f"not a {MATCH_SCHEMA} document with segments (run `slide-sync match`)")
    if not (0.0 <= min_confidence <= 1.0):
        raise SlideSyncError("INVALID_ARGUMENT", "--min-confidence must be in [0, 1]")
    deck_dir = deck_dir or (match_doc.get("deck") or {}).get("path")
    if not deck_dir:
        raise SlideSyncError("INVALID_DECK", "the match names no deck directory: pass --deck")
    deck = load_deck(deck_dir)
    pages = {int(p["index"]): p for p in deck["pages"]}
    out_path = os.path.abspath(output or (os.path.splitext(os.path.abspath(video))[0] + "_clean"
                                          + (os.path.splitext(video)[1] or ".mp4")))
    if out_path == os.path.abspath(video):
        raise SlideSyncError("OUTPUT_IS_INPUT", "-o must not be the input file")
    if "probe" not in skill.tools:
        raise EngineError("ENGINE_TOOL_MISSING", f"ffmpeg-skill {skill.version} lacks: probe")
    warnings: List[str] = []
    src = (match_doc.get("source") or {}).get("path")
    if src and os.path.abspath(src) != os.path.abspath(video):
        warnings.append(f"the match was measured on {src}, not on {os.path.abspath(video)}")
    if (match_doc.get("deck") or {}).get("path") and os.path.abspath(match_doc["deck"]["path"]) != os.path.abspath(deck_dir):
        warnings.append(f"the match was made with the deck in {match_doc['deck']['path']}, not {os.path.abspath(deck_dir)}")
    base = _probe(skill, video, timeout, runner)
    v = base["video"]
    width, height, duration = int(v["width"]), int(v["height"]), float(base["duration"])
    fps = float(v.get("fps") or 30.0)
    decisions = decide_segments(sorted(match_doc["segments"], key=lambda s: float(s["start"])), min_confidence=min_confidence)
    spans = replaced_spans(decisions, duration)
    for s in spans:
        if s["page"] not in pages:
            raise SlideSyncError("INVALID_DECK", f"page {s['page']} of the match is not in {deck_dir}")

    stem, ext = os.path.splitext(out_path)
    ext = ext or ".mp4"
    steps: List[Dict[str, Any]] = []
    clips: Dict[int, str] = {}
    for page in sorted({s["page"] for s in spans}):
        p = pages[page]
        w, h = page_size(float(p["width_px"]) / float(p["height_px"]), width, height)
        longest = max(s["duration"] for s in spans if s["page"] == page)
        clip = f"{stem}.clean-page-{page:03d}.mp4"
        image = os.path.abspath(os.path.join(deck_dir, p["image"]))
        clips[page] = clip
        steps.append({"tool": "insert", "what": f"page {page} as a {w}x{h} still clip", "input": image, "output": clip,
                      "args": [image, "--duration", f"{math.ceil((longest + CLIP_SLACK) * 1000) / 1000:.3f}", "--width",
                               str(w), "--height", str(h), "--fps", f"{fps:g}", "-o", clip], "intermediate": True})
    if spans:
        args = [os.path.abspath(video)]
        for s in spans:
            args += ["--insert", clips[s["page"]], "--at", f"{s['start']:.3f}", "--end", f"{s['end']:.3f}", "--from", "0"]
        args += ["--audio", "a", "--pad-color", pad_color, "-o", out_path]
        steps.append({"tool": "broll", "what": f"deck pages over {len(spans)} span(s) of the capture",
                      "input": os.path.abspath(video), "output": out_path, "args": args, "intermediate": False})
    else:
        warnings.append("nothing to replace: no segment matched a page with at least --min-confidence")
    for n, st in enumerate(steps, start=1):
        st["step"] = n
    steps = [dict([("step", st["step"])] + [(k, x) for k, x in st.items() if k != "step"]) for st in steps]
    counts = {r: sum(1 for d in decisions if d["rule"] == r) for r in RULES}
    return {
        "schema": CLEAN_SCHEMA,
        "source": {"path": os.path.abspath(video), "duration": _r(duration), "width": width, "height": height, "fps": fps},
        "deck": {"path": os.path.abspath(deck_dir), "page_count": len(pages)},
        "derived_from": {"schema": MATCH_SCHEMA, "path": os.path.abspath(match_path) if match_path else None, "source": src,
                         "segment_count": len(decisions)},
        "planner": {"skill": "slide-sync", "skill_version": __version__, "engine": "ffmpeg-skill",
                    "engine_version": skill.version, "min_confidence": min_confidence, "pad_color": pad_color,
                    "audio": "the recording's, throughout"},
        "rules": counts,
        "replaced_count": counts["confident_match"],
        "segments": decisions,
        "spans": spans,
        "steps": steps,
        "output": out_path if steps else None,
        "warnings": warnings,
        "provenance": "DERIVED",
    }
