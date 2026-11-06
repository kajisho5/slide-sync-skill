"""LAYOUT: how a slide talk is framed, segment by segment, as a plan for ffmpeg-skill `overlay` / `broll`.

A talk is often recorded twice: the slide picture (a screen capture, or a camera on the projection) and a camera on
the speaker. From `match` (which deck page is on screen when), every span of the recording gets one layout:

| Layout | Picture | ffmpeg-skill step |
|---|---|---|
| `slide_full` | the slide recording as it is | none |
| `slide_pip` | the slide, the speaker camera in a corner | `overlay --video <camera> --position <corner> --scale-percent <w> --place ...` |
| `side_by_side` | the slide, the speaker camera in the free band beside a pillarboxed slide (the slide is not covered) | `overlay --video <camera> --position X,Y --margin 0 --scale <band> --place ...` |
| `speaker_full` | the speaker camera instead of the slide picture | `broll --insert <camera> --at --end --from` |

Every segment says which rule chose its layout (docs/decisions.md ADR-005). The plan is the list of ffmpeg-skill calls
in order (each one's input is the previous one's output); `render_layout` runs it. The recording's audio is kept
throughout (`overlay` keeps the main audio, `broll --audio a`). This skill never runs ffmpeg itself.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .core import SlideSyncError
from .engine import EngineError, FfmpegSkill, Runner, run_tool, subprocess_runner
from .match import MATCH_SCHEMA, load_deck

LAYOUT_SCHEMA = "slide-sync/layout@1"
LAYOUTS = ("slide_full", "slide_pip", "side_by_side", "speaker_full")
SLIDE_MODES = ("auto", "pip", "side", "full")
UNMATCHED_MODES = ("keep", "speaker")
LAYOUT_TOOLS = ("probe", "overlay", "broll")
PLACE_MIN_VERSION = (2, 13, 0)   # overlay --place (show a range of the layer at a time) came in ffmpeg-skill 2.13.0
DEFAULT_PIP_POSITION = "bottom-right"
PIP_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")
DEFAULT_PIP_WIDTH = 25.0         # percent of the frame width
MIN_SIDE_BAND = 0.12             # a free band beside the slide at least this fraction of the frame width (4:3 in 16:9: 0.125)
MIN_SPAN = 0.05                  # seconds: a piece shorter than this (after splitting) is dropped into its neighbour

RULES = {
    "deck_page": "a deck page is on screen",
    "deck_page_side_band": "a deck page is on screen and the slide leaves a free band at the side",
    "deck_page_no_side_band": "a deck page is on screen; --slides side, but the slide leaves no free band wide enough",
    "slides_full": "a deck page is on screen; --slides full",
    "unmatched_mid_talk_kept": "no deck page matched between matched pages (a demo, a backup slide, a blank): "
                               "the slide picture is kept",
    "unmatched_speaker": "no deck page matched; --unmatched speaker",
    "before_first_deck_page": "before the first matched deck page (the talk has not started)",
    "after_last_deck_page": "after the last matched deck page (the talk is over: Q&A)",
    "no_deck_page": "no segment of the recording matched a deck page",
    "qa_from": "at or after --qa-from (Q&A)",
    "no_camera": "no speaker camera given: the slide picture is kept",
    "camera_not_covering": "the speaker camera does not cover this time: the slide picture is kept",
}


def _r(t: float) -> float:
    return round(float(t), 3) + 0.0


def _version_tuple(v: str) -> Optional[Tuple[int, ...]]:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _check_match(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(doc, dict) or doc.get("schema") != MATCH_SCHEMA or not doc.get("segments"):
        raise SlideSyncError("INVALID_ARGUMENT", f"not a {MATCH_SCHEMA} document with segments (run `slide-sync match`)")
    return sorted(doc["segments"], key=lambda s: float(s["start"]))


def _page_aspects(match_doc: Dict[str, Any]) -> Dict[int, float]:
    """Page index -> width / height, from the deck the match was made with (when it can still be read)."""
    deck_dir = (match_doc.get("deck") or {}).get("path")
    if not deck_dir:
        return {}
    try:
        deck = load_deck(deck_dir)
    except SlideSyncError:
        return {}
    out = {}
    for p in deck["pages"]:
        w, h = p.get("width_px"), p.get("height_px")
        if isinstance(w, (int, float)) and isinstance(h, (int, float)) and h > 0:
            out[int(p["index"])] = float(w) / float(h)
    return out


def side_band(view: Optional[str], page_aspect: Optional[float], width: int, height: int) -> int:
    """Width in px of the free band on each side of a slide shown pillarboxed in the frame (0 when there is none).
    Only a `fit` view says where the slide is: the page's aspect fitted and centred, as a screen capture of a 4:3 deck
    on a 16:9 output shows it. A `located` slide (a camera on the projection) or a `full` one gives no band."""
    if not view or not view.startswith("fit") or not page_aspect or not width or not height:
        return 0
    slide_w = height * page_aspect
    if slide_w >= width:
        return 0
    return int((width - slide_w) / 2) // 2 * 2   # even: ffmpeg-skill scales the layer to it


def assign(segments: Sequence[Dict[str, Any]], *, camera: bool, coverage: Optional[Tuple[float, float]] = None,
           qa_from: Optional[float] = None, slides: str = "auto", unmatched: str = "keep",
           page_aspects: Optional[Dict[int, float]] = None, width: int = 0, height: int = 0,
           duration: Optional[float] = None) -> List[Dict[str, Any]]:
    """One layout per piece of the recording, with the rule that chose it. Segments are split at --qa-from and at the
    edges of the camera's coverage, so every piece has one layout. Pure: no media is read."""
    if slides not in SLIDE_MODES:
        raise SlideSyncError("INVALID_ARGUMENT", f"--slides must be one of {', '.join(SLIDE_MODES)}")
    if unmatched not in UNMATCHED_MODES:
        raise SlideSyncError("INVALID_ARGUMENT", f"--unmatched must be one of {', '.join(UNMATCHED_MODES)}")
    page_aspects = page_aspects or {}
    matched = [i for i, s in enumerate(segments) if s.get("page") is not None]
    first, last = (matched[0], matched[-1]) if matched else (None, None)
    cuts = sorted({t for t in ([qa_from] if qa_from is not None else []) + (list(coverage) if coverage else [])})
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(segments):
        start, end = float(s["start"]), float(s["end"])
        if duration is not None:
            end = min(end, float(duration))
        edges = [start] + [t for t in cuts if start + MIN_SPAN < t < end - MIN_SPAN] + [end]
        for a, b in zip(edges, edges[1:]):
            if b - a <= 0:
                continue
            page = s.get("page")
            mid = (a + b) / 2
            band = 0
            if qa_from is not None and mid >= qa_from:
                layout, rule = "speaker_full", "qa_from"
            elif page is None and first is None:
                layout, rule = "speaker_full", "no_deck_page"
            elif page is None and i < first:
                layout, rule = "speaker_full", "before_first_deck_page"
            elif page is None and i > last:
                layout, rule = "speaker_full", "after_last_deck_page"
            elif page is None and unmatched == "speaker":
                layout, rule = "speaker_full", "unmatched_speaker"
            else:
                band = side_band(s.get("view"), page_aspects.get(page) if page is not None else None, width, height)
                if page is None:
                    rule = "unmatched_mid_talk_kept"
                    layout = "slide_pip"
                elif slides == "full":
                    layout, rule = "slide_full", "slides_full"
                elif slides == "pip":
                    layout, rule = "slide_pip", "deck_page"
                elif band > 0 and band >= MIN_SIDE_BAND * width:
                    layout, rule = "side_by_side", "deck_page_side_band"
                elif slides == "side":
                    layout, rule = "slide_pip", "deck_page_no_side_band"
                else:
                    layout, rule = "slide_pip", "deck_page"
                if slides == "full" and page is None:
                    layout = "slide_full"
            if layout != "slide_full" and not camera:
                layout, rule = "slide_full", "no_camera"
            elif layout != "slide_full" and coverage and not (coverage[0] - 1e-6 <= mid <= coverage[1] + 1e-6):
                layout, rule = "slide_full", "camera_not_covering"
            piece = {"segment": s.get("index"), "start": _r(a), "end": _r(b), "page": page,
                     "confidence": s.get("confidence"), "view": s.get("view"), "layout": layout, "rule": rule,
                     "why": RULES[rule]}
            if layout == "side_by_side":
                piece["band"] = band
            out.append(piece)
    return out


def spans_of(pieces: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Consecutive pieces with the same layout (and, side by side, the same band) as one span."""
    spans: List[Dict[str, Any]] = []
    for p in pieces:
        key = (p["layout"], p.get("band"))
        if spans and spans[-1]["_key"] == key and abs(spans[-1]["end"] - p["start"]) < 1e-6:
            spans[-1]["end"] = p["end"]
            spans[-1]["segments"].append(p["segment"])
            continue
        spans.append({"_key": key, "start": p["start"], "end": p["end"], "layout": p["layout"], "segments": [p["segment"]],
                      **({"band": p["band"]} if p.get("band") is not None else {})})
    for s in spans:
        del s["_key"]
        s["duration"] = _r(s["end"] - s["start"])
    return spans


def steps_for(video: str, camera: Optional[str], spans: Sequence[Dict[str, Any]], *, offset: float, output: str,
              width: int, height: int, camera_size: Tuple[int, int] = (16, 9), pip_position: str = DEFAULT_PIP_POSITION,
              pip_width: float = DEFAULT_PIP_WIDTH) -> List[Dict[str, Any]]:
    """The ffmpeg-skill calls that make the layout, in order: one `overlay` per corner / band size, then one `broll`
    for every speaker-full span. Each call reads the previous one's output; the last writes `output`."""
    if not camera:
        return []
    calls: List[Tuple[str, List[str], str]] = []   # tool, arguments without input / output, what it does

    def place(s: Dict[str, Any]) -> str:
        return f"{s['start'] + offset:.3f}-{s['end'] + offset:.3f}@{s['start']:.3f}"

    pip = [s for s in spans if s["layout"] == "slide_pip"]
    if pip:
        args = ["--video", camera, "--position", pip_position, "--scale-percent", f"{pip_width:g}"]
        calls.append(("overlay", args + [x for s in pip for x in ("--place", place(s))],
                      f"speaker camera picture-in-picture ({pip_position}, {pip_width:g}% wide) over {len(pip)} span(s)"))
    for band in sorted({s["band"] for s in spans if s["layout"] == "side_by_side"}):
        side = [s for s in spans if s["layout"] == "side_by_side" and s["band"] == band]
        # the band on the right of the slide, the camera centred in it vertically (overlay scales it to the band's
        # width with an even height)
        cam_w, cam_h = camera_size
        scaled_h = int(round(band * cam_h / cam_w / 2)) * 2 if cam_w else 0
        args = ["--video", camera, "--position", f"{width - band},{max(0, (height - scaled_h) // 2)}", "--margin", "0",
                "--scale", str(band)]
        calls.append(("overlay", args + [x for s in side for x in ("--place", place(s))],
                      f"speaker camera in the {band} px band beside the slide over {len(side)} span(s)"))
    full = [s for s in spans if s["layout"] == "speaker_full"]
    if full:
        args = []
        for s in full:
            args += ["--insert", camera, "--at", f"{s['start']:.3f}", "--end", f"{s['end']:.3f}",
                     "--from", f"{s['start'] + offset:.3f}"]
        calls.append(("broll", args + ["--audio", "a"], f"speaker camera full frame over {len(full)} span(s)"))
    stem, ext = os.path.splitext(os.path.abspath(output))
    ext = ext or ".mp4"
    steps = []
    src = os.path.abspath(video)
    for n, (tool, args, what) in enumerate(calls, start=1):
        dst = os.path.abspath(output) if n == len(calls) else f"{stem}.layout-step{n}{ext}"
        steps.append({"step": n, "tool": tool, "what": what, "input": src, "output": dst,
                      "args": [src] + args + ["-o", dst], "intermediate": n < len(calls)})
        src = dst
    return steps


def _probe(skill: FfmpegSkill, path: str, timeout: float, runner: Runner) -> Dict[str, Any]:
    doc = run_tool(skill, "probe", [path], timeout=timeout, runner=runner)
    v = doc.get("video") or {}
    if not isinstance(doc.get("duration"), (int, float)) or not (v.get("width") and v.get("height")):
        raise SlideSyncError("INVALID_ARGUMENT", f"{path} has no video stream ffmpeg-skill can measure")
    return doc


def plan_layout(skill: FfmpegSkill, video: str, match_doc: Dict[str, Any], *, camera: Optional[str] = None,
                camera_offset: float = 0.0, qa_from: Optional[float] = None, slides: str = "auto",
                unmatched: str = "keep", pip_position: str = DEFAULT_PIP_POSITION, pip_width: float = DEFAULT_PIP_WIDTH,
                output: Optional[str] = None, match_path: Optional[str] = None, timeout: float = 3600.0,
                runner: Runner = subprocess_runner) -> Dict[str, Any]:
    """The layout plan (`slide-sync/layout@1`): a layout and its rule for every piece of the recording, the spans, and
    the ffmpeg-skill calls that make it. Measures the recording and the camera with ffmpeg-skill `probe`; writes
    nothing."""
    if not os.path.isfile(video):
        raise SlideSyncError("INPUT_NOT_FOUND", f"no such file: {video}")
    if camera and not os.path.isfile(camera):
        raise SlideSyncError("INPUT_NOT_FOUND", f"no such file: {camera}")
    segments = _check_match(match_doc)
    if pip_position not in PIP_POSITIONS:
        raise SlideSyncError("INVALID_ARGUMENT", f"--pip-position must be one of {', '.join(PIP_POSITIONS)}")
    if not (5.0 <= pip_width <= 50.0):
        raise SlideSyncError("INVALID_ARGUMENT", "--pip-width must be 5 to 50 (percent of the frame width)")
    if qa_from is not None and qa_from < 0:
        raise SlideSyncError("INVALID_ARGUMENT", "--qa-from must not be negative")
    out_path = output or (os.path.splitext(os.path.abspath(video))[0] + "_layout" + (os.path.splitext(video)[1] or ".mp4"))
    if os.path.abspath(out_path) in (os.path.abspath(video), os.path.abspath(camera) if camera else None):
        raise SlideSyncError("OUTPUT_IS_INPUT", "-o must not be the recording or the camera")
    if "probe" not in skill.tools:
        raise EngineError("ENGINE_TOOL_MISSING", f"ffmpeg-skill {skill.version} lacks: probe")
    warnings: List[str] = []
    src = (match_doc.get("source") or {}).get("path")
    if src and os.path.abspath(src) != os.path.abspath(video):
        warnings.append(f"the match was measured on {src}, not on {os.path.abspath(video)}")
    base = _probe(skill, video, timeout, runner)
    v = base["video"]
    width, height, duration = int(v["width"]), int(v["height"]), float(base["duration"])
    cam_doc, coverage = None, None
    if camera:
        cam = _probe(skill, camera, timeout, runner)
        cam_dur = float((cam.get("video") or {}).get("duration") or cam["duration"])
        coverage = (max(0.0, -camera_offset), min(duration, cam_dur - camera_offset))
        cam_doc = {"path": os.path.abspath(camera), "duration": _r(cam_dur), "width": cam["video"]["width"],
                   "height": cam["video"]["height"], "offset": camera_offset,
                   "covers": [_r(coverage[0]), _r(coverage[1])] if coverage[1] > coverage[0] else None}
        if coverage[1] <= coverage[0]:
            warnings.append("the speaker camera does not overlap the recording at this --camera-offset")
            coverage = (duration + 1.0, duration + 1.0)   # covers nothing
        elif coverage[0] > 1e-3 or coverage[1] < duration - 1e-3:
            warnings.append(f"the speaker camera covers {coverage[0]:.3f}-{coverage[1]:.3f}s of the recording only; "
                            "outside it the slide picture is kept")
    pieces = assign(segments, camera=bool(camera), coverage=coverage, qa_from=qa_from, slides=slides, unmatched=unmatched,
                    page_aspects=_page_aspects(match_doc), width=width, height=height, duration=duration)
    if not any(s.get("page") is not None for s in segments):
        warnings.append("no segment matched a deck page: every span is the speaker camera (check the deck and the match)")
    spans = spans_of(pieces)
    steps = steps_for(video, camera, spans, offset=camera_offset, output=out_path, width=width, height=height,
                      camera_size=(int(cam_doc["width"]), int(cam_doc["height"])) if cam_doc else (16, 9),
                      pip_position=pip_position, pip_width=pip_width)
    if not steps:
        warnings.append("nothing to render: every span keeps the slide recording as it is")
    if steps and (_version_tuple(skill.version) or PLACE_MIN_VERSION) < PLACE_MIN_VERSION and any(
            s["tool"] == "overlay" for s in steps):
        warnings.append(f"ffmpeg-skill {skill.version} has no overlay --place (2.13.0 or later): this plan cannot be rendered")
    counts = {k: sum(1 for p in pieces if p["layout"] == k) for k in LAYOUTS}
    return {
        "schema": LAYOUT_SCHEMA,
        "source": {"path": os.path.abspath(video), "duration": _r(duration), "width": width, "height": height,
                   "fps": v.get("fps")},
        "camera": cam_doc,
        "derived_from": {"schema": MATCH_SCHEMA, "path": os.path.abspath(match_path) if match_path else None, "source": src,
                         "deck": (match_doc.get("deck") or {}).get("path"), "segment_count": len(segments),
                         "matched_count": sum(1 for s in segments if s.get("page") is not None)},
        "planner": {"skill": "slide-sync", "skill_version": __version__, "engine": "ffmpeg-skill",
                    "engine_version": skill.version, "slides": slides, "unmatched": unmatched, "qa_from": qa_from,
                    "pip_position": pip_position, "pip_width_percent": pip_width, "min_side_band": MIN_SIDE_BAND,
                    "audio": "the recording's, throughout"},
        "layouts": counts,
        "segments": pieces,
        "spans": spans,
        "steps": steps,
        "output": os.path.abspath(out_path) if steps else None,
        "warnings": warnings,
        "provenance": "DERIVED",
    }


def render_layout(skill: FfmpegSkill, plan: Dict[str, Any], *, overwrite: bool = False, timeout: float = 3600.0,
                  runner: Runner = subprocess_runner) -> Dict[str, Any]:
    """Run the plan's ffmpeg-skill calls in order. Intermediate files are removed afterwards; the input and the camera
    are only read. An existing output is refused without `overwrite`, an existing intermediate always."""
    steps = plan.get("steps") or []
    if not steps:
        raise SlideSyncError("INVALID_ARGUMENT", "the plan has no step to render: every span keeps the recording as it is")
    missing = [t for t in {s["tool"] for s in steps} if t not in skill.tools]
    if missing:
        raise EngineError("ENGINE_TOOL_MISSING", f"ffmpeg-skill {skill.version} lacks: {', '.join(sorted(missing))}")
    if any(s["tool"] == "overlay" for s in steps) and (_version_tuple(skill.version) or PLACE_MIN_VERSION) < PLACE_MIN_VERSION:
        raise EngineError("ENGINE_TOOL_MISSING", f"ffmpeg-skill {skill.version} has no overlay --place (needs 2.13.0 or later)")
    final = steps[-1]["output"]
    if os.path.exists(final) and not overwrite:
        raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {final}: pass --overwrite")
    for s in steps[:-1]:
        if os.path.exists(s["output"]):
            raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {s['output']} (an intermediate file name): "
                                                     "move it away first")
    done = []
    try:
        for s in steps:
            args = list(s["args"][:-2]) + ["-o", s["output"]] + (["--overwrite"] if s is steps[-1] and overwrite else [])
            doc = run_tool(skill, s["tool"], args, timeout=timeout, runner=runner)
            done.append({"step": s["step"], "tool": s["tool"], "status": doc.get("status"),
                         "verified": doc.get("verified"), "output": s["output"]})
    finally:
        for s in steps[:-1]:
            try:
                os.remove(s["output"])
            except OSError:
                pass
    return {"output": final, "steps": done}
