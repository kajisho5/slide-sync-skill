"""INSERT: splice clips into a talk recording at slide boundaries, keeping the slide timeline and chapters right.

A clip (a demo video, the video a slide embeds, or a still such as a deck page or a title card) goes in between two
slides: `--insert N=CLIP` puts it after slide N of the timeline (0: before the first slide). The media work is
ffmpeg-skill's, never this skill's (docs/decisions.md ADR-003):

1. `probe` the recording (frame size, frame rate, duration) and every video clip.
2. A still becomes a clip of `--still-duration` seconds at the recording's size and rate through `insert`.
3. The recording is cut at each boundary that has a clip (`cut --accurate`, frame-exact).
4. Parts and clips are joined in order with no transition (`join --transition none`), at the recording's size and rate;
   a clip of another size is padded, a clip without sound gets silence.
5. The timeline is shifted: every slide after an insertion moves later by the inserted clip's measured duration, and
   each clip becomes a segment of its own (`kind: insert`). Chapters come from that timeline and go into the output
   through `metadata` (streams copied).

The shifted timeline is `slide-sync/timeline@1` with `provenance: DERIVED`, `derived_from` (the measured timeline it
came from) and `edits` (every insertion, where it went, its measured length, the clip's sha256) plus `render` (each
ffmpeg-skill step and the measured drift of the output from the predicted length).
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .core import TIMELINE_SCHEMA, SlideSyncError, chapters, write_chapters
from .engine import INSERT_TOOLS, EngineError, FfmpegSkill, Runner, run_tool, subprocess_runner

DEFAULT_STILL_DURATION = 5.0
MAX_STILL_DURATION = 3600.0
STILL_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
EPS = 1e-3  # seconds: a boundary this close to the start or the end of the recording is that end (no cut)


def _r(t: float) -> float:
    return round(float(t), 3)


def parse_insert(spec: str) -> Tuple[int, str]:
    """`N=CLIP` -> (N, CLIP): insert CLIP after slide N (0 = before the first slide)."""
    head, sep, path = spec.partition("=")
    if not sep or not path or not head.strip().isdigit():
        raise SlideSyncError("INVALID_ARGUMENT", f"--insert takes N=CLIP (insert after slide N, 0 = before slide 1): {spec!r}")
    return int(head), path


def clip_kind(path: str) -> str:
    return "still" if os.path.splitext(path)[1].lower() in STILL_EXTS else "video"


def boundary(slides: Sequence[Dict[str, Any]], after: int) -> float:
    """Source time of the boundary after slide `after` (1-based position; 0 is the start of the recording)."""
    return 0.0 if after == 0 else float(slides[after - 1]["end"])


def _check_timeline(timeline: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(timeline, dict) or timeline.get("schema") != TIMELINE_SCHEMA or not timeline.get("slides"):
        raise SlideSyncError("INVALID_TIMELINE", f"not a {TIMELINE_SCHEMA} document with slides")
    return list(timeline["slides"])


def shift_timeline(timeline: Dict[str, Any], inserts: Sequence[Dict[str, Any]], *,
                   output: Optional[str] = None) -> Dict[str, Any]:
    """The timeline of the recording with `inserts` spliced in. Each insert is a dict with `after_slide` (0..n),
    `duration` (seconds, > 0) and optionally `clip`, `title`, `kind`, `sha256`. Inserts at the same boundary go in in
    the order given. Every slide after an insertion moves later by its duration; each insert becomes a segment of its
    own (`kind: insert`); segments are renumbered 1..m and slides keep their `source_slide`."""
    slides = _check_timeline(timeline)
    n = len(slides)
    for j, ins in enumerate(inserts):
        k = ins.get("after_slide")
        if not isinstance(k, int) or isinstance(k, bool) or not 0 <= k <= n:
            raise SlideSyncError("INVALID_ARGUMENT", f"insert {j + 1}: after_slide must be 0 to {n} (the timeline has {n} slide(s))")
        d = ins.get("duration")
        if not isinstance(d, (int, float)) or isinstance(d, bool) or not d > 0:
            raise SlideSyncError("INVALID_ARGUMENT", f"insert {j + 1}: duration must be above 0")
    by_slide: Dict[int, List[int]] = {}
    for j, ins in enumerate(inserts):
        by_slide.setdefault(ins["after_slide"], []).append(j)

    segments: List[Dict[str, Any]] = []
    edits: List[Dict[str, Any]] = []
    shift = 0.0

    def splice(k: int) -> None:
        nonlocal shift
        for j in by_slide.get(k, []):
            ins, at = inserts[j], boundary(slides, k)
            d = float(ins["duration"])
            start = at + shift
            title = ins.get("title") or (os.path.splitext(os.path.basename(ins["clip"]))[0] if ins.get("clip") else f"Insert {j + 1}")
            segments.append({"kind": "insert", "start": _r(start), "end": _r(start + d), "duration": _r(d),
                             "insert": j + 1, "title": title})
            edits.append({"op": "insert", "insert": j + 1, "after_slide": k, "at_source": _r(at), "at_output": _r(start),
                          "duration": _r(d), "shift_after": _r(shift + d), "title": title,
                          "clip": os.path.abspath(ins["clip"]) if ins.get("clip") else None,
                          "clip_kind": ins.get("kind"), "clip_sha256": ins.get("sha256")})
            shift += d

    splice(0)
    for pos, s in enumerate(slides, start=1):
        seg = {k: v for k, v in s.items() if k not in ("index",)}
        seg.setdefault("kind", "slide")
        if seg["kind"] == "slide":
            seg.setdefault("source_slide", s.get("index", pos))
        seg.update(start=_r(float(s["start"]) + shift), end=_r(float(s["end"]) + shift), duration=_r(float(s["end"]) - float(s["start"])))
        segments.append(seg)
        splice(pos)
    for i, seg in enumerate(segments, start=1):
        seg["index"] = i
    segments = [dict([("index", seg["index"])] + [(k, v) for k, v in seg.items() if k != "index"]) for seg in segments]

    src = timeline.get("source") or {}
    total = sum(float(i["duration"]) for i in inserts)
    out = {
        "schema": TIMELINE_SCHEMA,
        "source": {"path": os.path.abspath(output) if output else None,
                   "duration": _r(float(src["duration"]) + total) if isinstance(src.get("duration"), (int, float)) else None},
        "detector": timeline.get("detector"),
        "slide_count": len(segments),
        "inserted_count": len(inserts),
        "changes": [s["start"] for s in segments[1:]],
        "slides": segments,
        "sheet": None,
        "provenance": "DERIVED",
        "derived_from": {"path": src.get("path"), "duration": src.get("duration"),
                         "provenance": timeline.get("provenance"), "slide_count": n},
        "edits": list(timeline.get("edits") or []) + edits,
    }
    return out


def segment_titles(derived: Dict[str, Any], titles: Sequence[str], prefix: str = "Slide") -> List[str]:
    """One title per segment of a shifted timeline: a slide keeps the title of its source slide (`titles` in order,
    then `prefix N` with N its number in the recording), an inserted clip its own title."""
    out = []
    for s in derived["slides"]:
        if s.get("kind") == "insert":
            out.append(s.get("title") or f"Insert {s.get('insert')}")
        else:
            n = int(s.get("source_slide") or s["index"])
            out.append(titles[n - 1] if n - 1 < len(titles) else f"{prefix} {n}")
    return out


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _probe(skill: FfmpegSkill, path: str, timeout: float, runner: Runner) -> Dict[str, Any]:
    doc = run_tool(skill, "probe", [path], timeout=timeout, runner=runner)
    if not isinstance(doc.get("duration"), (int, float)) or doc["duration"] <= 0:
        raise SlideSyncError("ENGINE_FAILED", f"ffmpeg-skill probe gave no duration for {path}")
    return doc


def insert_clips(skill: FfmpegSkill, video: str, timeline: Dict[str, Any], inserts: Sequence[Tuple[int, str]],
                 output: str, *, titles: Optional[Sequence[str]] = None, insert_titles: Optional[Sequence[str]] = None,
                 prefix: str = "Slide", min_chapter: float = 0.0, still_duration: float = DEFAULT_STILL_DURATION,
                 overwrite: bool = False, timeout: float = 3600.0, runner: Runner = subprocess_runner) -> Dict[str, Any]:
    """Splice `inserts` ((after_slide, clip) pairs) into `video` and write the result, with chapters, to `output`.
    Returns the shifted timeline (provenance DERIVED, with `edits` and `render`), the chapters and warnings."""
    if not os.path.isfile(video):
        raise SlideSyncError("INPUT_NOT_FOUND", f"no such file: {video}")
    slides = _check_timeline(timeline)
    if not inserts:
        raise SlideSyncError("INVALID_ARGUMENT", "nothing to insert: give --insert N=CLIP at least once")
    if not (0 < still_duration <= MAX_STILL_DURATION):
        raise SlideSyncError("INVALID_ARGUMENT", f"--still-duration must be in (0, {MAX_STILL_DURATION:g}]")
    out_abs = os.path.abspath(output)
    if out_abs == os.path.abspath(video):
        raise SlideSyncError("OUTPUT_IS_INPUT", "-o must not be the input file")
    if any(os.path.abspath(clip) == out_abs for _, clip in inserts):
        raise SlideSyncError("OUTPUT_IS_INPUT", "-o must not be one of the clips")
    if os.path.exists(output) and not overwrite:
        raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {output}: pass --overwrite")
    n = len(slides)
    for k, clip in inserts:
        if not 0 <= k <= n:
            raise SlideSyncError("INVALID_ARGUMENT", f"--insert {k}={clip}: slide must be 0 to {n} (the timeline has {n} slide(s))")
        if not os.path.isfile(clip):
            raise SlideSyncError("INPUT_NOT_FOUND", f"no such file: {clip}")
    missing = [t for t in INSERT_TOOLS if t not in skill.tools]
    if missing:
        raise EngineError("ENGINE_TOOL_MISSING", f"ffmpeg-skill {skill.version} lacks: {', '.join(missing)}")
    warnings: List[str] = []
    src_tl = (timeline.get("source") or {}).get("path")
    if src_tl and os.path.abspath(src_tl) != os.path.abspath(video):
        warnings.append(f"the timeline was measured on {src_tl}, not on {os.path.abspath(video)}")

    steps: List[Dict[str, Any]] = []
    base = _probe(skill, video, timeout, runner)
    v = base.get("video") or {}
    width, height, fps = v.get("width"), v.get("height"), v.get("fps")
    if not (width and height and fps):
        raise SlideSyncError("INVALID_ARGUMENT", f"{video} has no video stream ffmpeg-skill can measure")
    duration = float(base["duration"])
    steps.append({"tool": "probe", "input": os.path.abspath(video), "duration": _r(duration), "width": width,
                  "height": height, "fps": fps})
    ext = os.path.splitext(output)[1] or ".mp4"
    insert_titles = list(insert_titles or [])

    with tempfile.TemporaryDirectory(prefix="slide_sync_insert_") as tmp:
        tmp = os.path.abspath(tmp)
        # 1. every clip, measured (a still made into a clip first)
        planned: List[Dict[str, Any]] = []
        for j, (k, clip) in enumerate(inserts):
            kind = clip_kind(clip)
            if kind == "still":
                made = os.path.join(tmp, f"insert-{j + 1:03d}.mp4")
                doc = run_tool(skill, "insert", [clip, "--duration", f"{still_duration:g}", "--width", str(width),
                                                 "--height", str(height), "--fps", f"{fps:g}", "-o", made],
                               timeout=timeout, runner=runner)
                probe = doc.get("probe") if isinstance(doc.get("probe"), dict) else None
                if not probe or not isinstance(probe.get("duration"), (int, float)):
                    probe = _probe(skill, made, timeout, runner)
                steps.append({"tool": "insert", "input": os.path.abspath(clip), "requested_duration": still_duration,
                              "duration": _r(probe["duration"])})
                media = made
            else:
                probe = _probe(skill, clip, timeout, runner)
                if not probe.get("video"):
                    raise SlideSyncError("INVALID_ARGUMENT", f"{clip} has no video stream (an image needs a still extension: "
                                                             + ", ".join(STILL_EXTS) + ")")
                steps.append({"tool": "probe", "input": os.path.abspath(clip), "duration": _r(probe["duration"])})
                media = clip
            planned.append({"after_slide": k, "clip": clip, "media": media, "kind": kind,
                            "duration": float(probe["duration"]), "sha256": _sha256(clip),
                            "title": insert_titles[j] if j < len(insert_titles) and insert_titles[j] else None})

        # 2. the recording cut at each boundary that gets a clip
        cuts = sorted({_r(boundary(slides, p["after_slide"])) for p in planned})
        cuts = [t for t in cuts if EPS < t < duration - EPS]
        edges = [0.0] + cuts + [duration]
        parts: List[str] = []
        if not cuts:
            parts.append(video)
        else:
            for i, (a, b) in enumerate(zip(edges, edges[1:])):
                part = os.path.join(tmp, f"part-{i + 1:03d}{ext}")
                args = [video, "--start", f"{a:.3f}"] + (["--end", f"{b:.3f}"] if i < len(edges) - 2 else []) + \
                       ["--accurate", "-o", part]
                doc = run_tool(skill, "cut", args, timeout=timeout, runner=runner)
                got = (doc.get("probe") or {}).get("duration") if isinstance(doc.get("probe"), dict) else None
                steps.append({"tool": "cut", "start": _r(a), "end": _r(b), "requested_duration": _r(b - a),
                              "duration": _r(got) if isinstance(got, (int, float)) else doc.get("output_duration"),
                              "mode": doc.get("mode")})
                parts.append(part)

        # 3. join order: clips at the start, then each part followed by the clips at its end
        sequence = _order(planned, slides, cuts, parts, duration)

        joined = os.path.join(tmp, f"joined{ext}")
        doc = run_tool(skill, "join", sequence + ["--transition", "none", "--width", str(width), "--height", str(height),
                                                  "--fps", f"{fps:g}", "-o", joined], timeout=timeout, runner=runner)
        measured = (doc.get("probe") or {}).get("duration") if isinstance(doc.get("probe"), dict) else None
        predicted = duration + sum(p["duration"] for p in planned)
        drift = _r(measured - predicted) + 0.0 if isinstance(measured, (int, float)) else None  # + 0.0: no -0.0
        steps.append({"tool": "join", "inputs": len(sequence), "transition": "none", "width": width, "height": height,
                      "fps": fps, "predicted_duration": _r(predicted),
                      "duration": _r(measured) if isinstance(measured, (int, float)) else None, "drift": drift,
                      "notes": doc.get("notes") or []})
        if drift is not None and abs(drift) > 1.5 / float(fps):
            warnings.append(f"the output is {drift:+.3f}s off the predicted length ({_r(predicted)}s): chapter times may be "
                            "off by about that much")

        # 4. the shifted timeline, its chapters, written into the output
        derived = shift_timeline(timeline, [{"after_slide": p["after_slide"], "duration": p["duration"], "clip": p["clip"],
                                             "kind": p["kind"], "sha256": p["sha256"], "title": p["title"]}
                                            for p in planned], output=output)
        titles = list(titles or [])
        if titles and len(titles) != n:
            warnings.append(f"{len(titles)} title(s) for {n} slide(s): titles are used in order")
        ch = chapters(derived, titles=segment_titles(derived, titles, prefix), prefix=prefix, min_chapter=min_chapter)
        written = write_chapters(skill, joined, ch, output, overwrite=overwrite, timeout=timeout, runner=runner)
        steps.append({"tool": "metadata", "chapters": len(ch["chapters"]), "output": out_abs})

    derived["render"] = {"engine": "ffmpeg-skill", "engine_version": skill.version, "steps": steps, "output": out_abs,
                         "drift": drift, "engine_status": written.get("engine_status")}
    return {"timeline": derived, "chapters": ch["chapters"], "text": ch["text"],
            "warnings": warnings + ch["warnings"], "output": out_abs}


def _order(planned: Sequence[Dict[str, Any]], slides: Sequence[Dict[str, Any]], cuts: Sequence[float],
           parts: Sequence[str], duration: float) -> List[str]:
    """Join order: the clips at the start of the recording, then each part followed by the clips at its end, every
    group in the order the inserts were given."""
    def place(p: Dict[str, Any]) -> int:
        t = boundary(slides, p["after_slide"])
        if t <= EPS:
            return -1                      # before the first part
        if t >= duration - EPS:
            return len(parts) - 1          # after the last part
        return list(cuts).index(_r(t))     # after part i, which ends at cuts[i]

    seq = [p["media"] for p in planned if place(p) == -1]
    for i, part in enumerate(parts):
        seq.append(part)
        seq += [p["media"] for p in planned if place(p) == i]
    return seq
