"""slide-sync CLI. Every command prints one JSON document with --json.

  slide-sync detect talk.mp4 --json                      # slide timeline
  slide-sync detect talk.mp4 --sheet sheet.png -o timeline.json
  slide-sync detect recording.ts --follow --json --chapters-out chapters.txt -o timeline.json   # while it records
  slide-sync chapters talk.mp4 --timeline timeline.json --chapters-out chapters.txt
  slide-sync chapters talk.mp4 --titles titles.txt --min-chapter 10 -o talk_chapters.mp4
  slide-sync import deck.pdf -o pages/ --json            # page PNGs + page text (pptx/key via LibreOffice)
  slide-sync match talk.mp4 --deck pages/ --timeline timeline.json -o match.json   # which page is on screen when
  slide-sync insert talk.mp4 --timeline timeline.json --insert 3=demo.mp4 --insert 5=pages/page-006.png -o talk_demo.mp4
  slide-sync layout talk.mp4 --match match.json --camera speaker.mp4 --plan-out layout.json -o talk_layout.mp4
  slide-sync clean camera.mp4 --match match.json --plan-out clean.json -o camera_clean.mp4   # deck pages over the capture
  slide-sync contract --json
  slide-sync doctor --json

Exit codes: 0 done, 2 invalid input or arguments, 3 ffmpeg-skill, pypdfium2 or LibreOffice not found (or missing a tool),
4 ffmpeg-skill or LibreOffice failed.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from typing import Any, Dict, List, Optional

from . import __version__
from .contract import ERRORS, contract
from .clean import CLEAN_TOOLS, DEFAULT_MIN_CONFIDENCE, plan_clean
from .core import (DEFAULT_MIN_DWELL, DEFAULT_THRESHOLD, SlideSyncError, chapters, detect, read_titles, timecode,
                   write_chapters)
from .follow import DEFAULT_POLL, DEFAULT_SETTLE, follow
from .deck import DEFAULT_DPI, MAX_DPI, MIN_DPI, import_deck, pdfium_status, soffice_status
from .engine import INSERT_TOOLS, REQUIRED_TOOLS, EngineError, FfmpegSkill, candidates, engine_doctor, locate
from .insert import DEFAULT_STILL_DURATION, insert_clips, parse_insert
from .layout import (DEFAULT_PIP_POSITION, DEFAULT_PIP_WIDTH, LAYOUT_TOOLS, PIP_POSITIONS, SLIDE_MODES,
                     UNMATCHED_MODES, plan_layout, render_layout)
from .match import DEFAULT_MIN_MARGIN, DEFAULT_MIN_SCORE, DEFAULT_SAMPLES, MAX_SAMPLES, match


def _emit(doc: Dict[str, Any], as_json: bool, human: str = "") -> None:
    if as_json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    elif human:
        print(human)


def _fail(code: str, message: str, as_json: bool, detail: Optional[str] = None) -> int:
    doc = {"ok": False, "error": {"code": code, "message": message, "detail": detail}}
    if as_json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        print(f"error: {message}" + (f" ({detail})" if detail else ""), file=sys.stderr)
    return ERRORS.get(code, {}).get("exit_code", 1)


def _engine(explicit: Optional[str]) -> FfmpegSkill:
    skill = locate(explicit)
    if skill is None:
        raise EngineError("ENGINE_NOT_FOUND", "ffmpeg-skill not found; pass --ffmpeg-skill DIR or set FFMPEG_SKILL_DIR",
                          "looked in: " + ", ".join(candidates(explicit)))
    missing = [t for t in REQUIRED_TOOLS if t not in skill.tools]
    if missing:
        raise EngineError("ENGINE_TOOL_MISSING", f"ffmpeg-skill {skill.version} lacks: {', '.join(missing)}")
    return skill


def _write_json(path: str, doc: Dict[str, Any], overwrite: bool) -> None:
    if os.path.exists(path) and not overwrite:
        raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {path}: pass --overwrite")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)   # a reader never sees half a file


def cmd_follow(a: argparse.Namespace) -> int:
    for path in (a.output, a.chapters_out):  # before the recording is followed
        if path and os.path.exists(path) and not a.overwrite:
            raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {path}: pass --overwrite")
    if a.sheet:
        raise SlideSyncError("INVALID_ARGUMENT", "--sheet is not available with --follow: run detect --sheet once it closed")
    skill = _engine(a.ffmpeg_skill)
    changes: List[float] = []

    def emit(ev: Dict[str, Any]) -> None:
        if ev["event"] == "change":
            changes.append(ev["time"])
        elif ev["event"] == "retract":
            changes.remove(ev["time"])
        if a.chapters_out and ev["event"] in ("change", "retract"):
            starts = [0.0] + sorted(changes)
            _write_text(a.chapters_out, "".join(f"{timecode(t)} Slide {i + 1}\n" for i, t in enumerate(starts)))
        if a.json:
            print(json.dumps(ev, ensure_ascii=False), flush=True)
        elif ev["event"] == "change":
            print(f"slide {ev['slide']:>3} at {ev['time']:>9.3f}s" + ("" if ev["while_recording"] else " (at close)"), flush=True)
        elif ev["event"] in ("waiting", "retract"):
            print(f"{ev['event']}: {ev.get('reason')}", flush=True)
        elif ev["event"] == "done":
            print(f"{'closed' if ev['closed'] else 'stopped'}: {ev['timeline']['slide_count']} slide(s)", flush=True)

    tl = follow(skill, a.video, emit, threshold=a.threshold, min_dwell=a.min_dwell, settle=a.settle, poll=a.poll,
                follow_for=a.follow_for, timeout=a.timeout)
    if a.chapters_out:
        _write_text(a.chapters_out, chapters(tl)["text"])
    if a.output:
        _write_json(a.output, tl, True)
    return 0


def cmd_detect(a: argparse.Namespace) -> int:
    if a.follow:
        return cmd_follow(a)
    skill = _engine(a.ffmpeg_skill)
    tl = detect(skill, a.video, threshold=a.threshold, min_dwell=a.min_dwell, sheet=a.sheet, timeout=a.timeout)
    if a.output:
        _write_json(a.output, tl, a.overwrite)
    if a.chapters_out:
        if os.path.exists(a.chapters_out) and not a.overwrite:
            raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {a.chapters_out}: pass --overwrite")
        _write_text(a.chapters_out, chapters(tl)["text"])
    lines = [f"{tl['slide_count']} slide(s) over {tl['source']['duration']:.1f}s"]
    lines += [f"  {s['index']:>3}  {s['start']:>9.3f}  {s['duration']:>8.3f}s" for s in tl["slides"]]
    _emit(dict(tl, ok=True, timeline_file=a.output), a.json, "\n".join(lines))
    return 0


def _timeline(a: argparse.Namespace, skill: FfmpegSkill) -> Dict[str, Any]:
    """--timeline when given, otherwise `detect` now with --threshold / --min-dwell."""
    if a.timeline:
        try:
            with open(a.timeline, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError) as e:
            raise SlideSyncError("INVALID_TIMELINE", f"cannot read {a.timeline}: {e}")
    return detect(skill, a.video, threshold=a.threshold, min_dwell=a.min_dwell, timeout=a.timeout)


def cmd_chapters(a: argparse.Namespace) -> int:
    skill = _engine(a.ffmpeg_skill)
    tl = _timeline(a, skill)
    ch = chapters(tl, titles=read_titles(a.titles), prefix=a.prefix, min_chapter=a.min_chapter)
    if a.chapters_out:
        if os.path.exists(a.chapters_out) and not a.overwrite:
            raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {a.chapters_out}: pass --overwrite")
        with open(a.chapters_out, "w", encoding="utf-8") as fh:
            fh.write(ch["text"])
    written = write_chapters(skill, a.video, ch, a.output, overwrite=a.overwrite, timeout=a.timeout) if a.output else None
    doc = {"ok": True, "video": os.path.abspath(a.video), "chapter_count": len(ch["chapters"]), "chapters": ch["chapters"],
           "text": ch["text"], "warnings": ch["warnings"], "chapters_file": a.chapters_out, "output": written}
    human = ch["text"] + "".join(f"warning: {w}\n" for w in ch["warnings"]) + (f"wrote {written['output']}\n" if written else "")
    _emit(doc, a.json, human.rstrip("\n"))
    return 0


def cmd_import(a: argparse.Namespace) -> int:
    m = import_deck(a.deck, a.output, dpi=a.dpi, overwrite=a.overwrite, soffice=a.soffice, timeout=a.timeout)
    out_dir = os.path.abspath(a.output)
    lines = [f"{m['page_count']} page(s) at {m['renderer']['dpi']:g} dpi -> {out_dir}"]
    lines += [f"  {p['index']:>3}  {p['image']}  {p['width_px']}x{p['height_px']}  {p['chars']} chars" for p in m["pages"]]
    _emit(dict(m, ok=True, output_dir=out_dir), a.json, "\n".join(lines))
    return 0


def cmd_match(a: argparse.Namespace) -> int:
    skill = _engine(a.ffmpeg_skill)
    if a.output and os.path.exists(a.output) and not a.overwrite:  # before the slow part
        raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {a.output}: pass --overwrite")
    tl = _timeline(a, skill)
    m = match(skill, a.video, a.deck, tl, min_score=a.min_score, min_margin=a.min_margin, samples=a.samples,
              timeout=a.timeout)
    if a.output:
        _write_json(a.output, m, a.overwrite)
    lines = [f"{m['matched_count']} of {m['segment_count']} segment(s) matched to a page of {m['deck']['page_count']}"]
    lines += [f"  {s['index']:>3}  {s['start']:>9.3f}  {s['end']:>9.3f}  "
              + (f"page {s['page']:<4} {s['confidence']:.3f}" if s["page"] is not None else f"--        {s['reason']}")
              for s in m["segments"]]
    lines += [f"warning: {w}" for w in m["warnings"]]
    _emit(dict(m, ok=True, match_file=a.output), a.json, "\n".join(lines))
    return 0


def cmd_insert(a: argparse.Namespace) -> int:
    inserts = [parse_insert(spec) for spec in a.insert]
    for path in (a.timeline_out, a.chapters_out):  # before the slow part
        if path and os.path.exists(path) and not a.overwrite:
            raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {path}: pass --overwrite")
    skill = _engine(a.ffmpeg_skill)
    tl = _timeline(a, skill)
    r = insert_clips(skill, a.video, tl, inserts, a.output, titles=read_titles(a.titles), insert_titles=a.insert_title,
                     prefix=a.prefix, min_chapter=a.min_chapter, still_duration=a.still_duration, overwrite=a.overwrite,
                     timeout=a.timeout)
    if a.timeline_out:
        _write_json(a.timeline_out, r["timeline"], a.overwrite)
    if a.chapters_out:
        with open(a.chapters_out, "w", encoding="utf-8") as fh:
            fh.write(r["text"])
    doc = {"ok": True, "video": os.path.abspath(a.video), "output": r["output"], "inserted_count": len(inserts),
           "chapter_count": len(r["chapters"]), "chapters": r["chapters"], "text": r["text"], "warnings": r["warnings"],
           "timeline": r["timeline"], "timeline_file": a.timeline_out, "chapters_file": a.chapters_out}
    human = r["text"] + "".join(f"warning: {w}\n" for w in r["warnings"]) + f"wrote {r['output']}\n"
    _emit(doc, a.json, human.rstrip("\n"))
    return 0


def cmd_layout(a: argparse.Namespace) -> int:
    for path in (a.plan_out,):  # before the slow part
        if path and os.path.exists(path) and not a.overwrite:
            raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {path}: pass --overwrite")
    if a.output and os.path.exists(a.output) and not a.overwrite:
        raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {a.output}: pass --overwrite")
    if bool(a.match) == bool(a.deck):
        raise SlideSyncError("INVALID_ARGUMENT", "give --match (a match JSON) or --deck (to match now), not both")
    skill = _engine(a.ffmpeg_skill)
    if a.match:
        try:
            with open(a.match, encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError) as e:
            raise SlideSyncError("INVALID_ARGUMENT", f"cannot read {a.match}: {e}")
    else:
        m = match(skill, a.video, a.deck, _timeline(a, skill), timeout=a.timeout)
    plan = plan_layout(skill, a.video, m, camera=a.camera, camera_offset=a.camera_offset, qa_from=a.qa_from,
                       slides=a.slides, unmatched=a.unmatched, pip_position=a.pip_position, pip_width=a.pip_width,
                       output=a.output, match_path=a.match, timeout=a.timeout)
    if a.plan_out:
        _write_json(a.plan_out, plan, a.overwrite)
    rendered = render_layout(skill, plan, overwrite=a.overwrite, timeout=a.timeout) if a.output and plan["steps"] else None
    lines = [f"{p['start']:>9.3f}  {p['end']:>9.3f}  {p['layout']:<13} {p['rule']}" for p in plan["segments"]]
    lines += [f"step {s['step']}: ffmpeg-skill {s['tool']} - {s['what']}" for s in plan["steps"]]
    lines += [f"warning: {w}" for w in plan["warnings"]] + ([f"wrote {rendered['output']}"] if rendered else [])
    _emit(dict(plan, ok=True, plan_file=a.plan_out, rendered=rendered), a.json, "\n".join(lines))
    return 0


def cmd_clean(a: argparse.Namespace) -> int:
    for path in (a.plan_out, a.output):  # before the slow part
        if path and os.path.exists(path) and not a.overwrite:
            raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {path}: pass --overwrite")
    if not a.match and not a.deck:
        raise SlideSyncError("INVALID_ARGUMENT", "give --match (a match JSON) or --deck (to match now)")
    skill = _engine(a.ffmpeg_skill)
    if a.match:
        try:
            with open(a.match, encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError) as e:
            raise SlideSyncError("INVALID_ARGUMENT", f"cannot read {a.match}: {e}")
    else:
        m = match(skill, a.video, a.deck, _timeline(a, skill), timeout=a.timeout)
    plan = plan_clean(skill, a.video, m, deck_dir=a.deck, min_confidence=a.min_confidence, pad_color=a.pad_color,
                      output=a.output, match_path=a.match, timeout=a.timeout)
    if a.plan_out:
        _write_json(a.plan_out, plan, a.overwrite)
    rendered = render_layout(skill, plan, overwrite=a.overwrite, timeout=a.timeout) if a.output and plan["steps"] else None
    lines = [f"{d['start']:>9.3f}  {d['end']:>9.3f}  {d['action']:<8} {d['rule']}"
             + (f" (page {d['page']}, {d['confidence']:.3f})" if d["page"] is not None else "") for d in plan["segments"]]
    lines += [f"step {s['step']}: ffmpeg-skill {s['tool']} - {s['what']}" for s in plan["steps"]]
    lines += [f"warning: {w}" for w in plan["warnings"]] + ([f"wrote {rendered['output']}"] if rendered else [])
    _emit(dict(plan, ok=True, plan_file=a.plan_out, rendered=rendered), a.json, "\n".join(lines))
    return 0


def cmd_contract(a: argparse.Namespace) -> int:
    _emit(contract(), True)
    return 0


def cmd_doctor(a: argparse.Namespace) -> int:
    skill = locate(a.ffmpeg_skill)
    engine: Dict[str, Any] = {"found": skill is not None, "root": None, "version": None, "missing_tools": list(REQUIRED_TOOLS),
                              "ffmpeg": None, "ffprobe": None, "ok": False}
    problems: List[str] = []
    if skill is None:
        problems.append("ffmpeg-skill not found (" + ", ".join(candidates(a.ffmpeg_skill)) + ")")
    else:
        engine.update(root=skill.root, version=skill.version, missing_tools=[t for t in REQUIRED_TOOLS if t not in skill.tools])
        if engine["missing_tools"]:
            problems.append("ffmpeg-skill lacks: " + ", ".join(engine["missing_tools"]))
        d = engine_doctor(skill)
        engine.update(ffmpeg=d["ffmpeg"], ffprobe=d["ffprobe"], tools=d["tools"], insert_tools=d["insert_tools"])
        if not d["ok"]:
            problems.append(d["detail"] or "ffmpeg-skill doctor reports a problem (ffmpeg, ffprobe or a needed tool)")
        engine["ok"] = not problems
    # Slide import is optional: a missing pypdfium2 or LibreOffice is reported (with how to install it) but does not
    # make doctor fail, because detect and chapters do not need them.
    pdf, office = pdfium_status(), soffice_status(a.soffice)
    slide_import = {"pdf": dict(pdf, ready=pdf["found"]), "libreoffice": office,
                    "ready": {"pdf": pdf["found"], "pptx_key_odp": pdf["found"] and office["found"]}}
    notes = [f"slide import: {name} not found; {d['install']}" for name, d in (("pypdfium2", pdf), ("LibreOffice", office))
             if not d["found"]]
    # Inserting clips needs more ffmpeg-skill tools than detect / chapters / match; missing ones are a note too.
    if skill is not None:
        unusable = [t for t in INSERT_TOOLS if t not in skill.tools or (engine.get("insert_tools") or {}).get(t) != "yes"]
        engine["insert_ready"] = not unusable
        if unusable:
            notes.append("insert: ffmpeg-skill tool(s) not usable: " + ", ".join(unusable))
        # `layout` renders through overlay / broll (plans need only probe); a missing one is a note as well.
        lay_missing = [t for t in LAYOUT_TOOLS if t not in skill.tools]
        engine["layout_ready"] = not lay_missing
        if lay_missing:
            notes.append("layout: ffmpeg-skill tool(s) missing: " + ", ".join(lay_missing))
        clean_missing = [t for t in CLEAN_TOOLS if t not in skill.tools]
        engine["clean_ready"] = not clean_missing
        if clean_missing:
            notes.append("clean: ffmpeg-skill tool(s) missing: " + ", ".join(clean_missing))
    doc = {"ok": not problems, "skill": "slide-sync-skill", "version": __version__,
           "python": platform.python_version(), "engine": engine, "import": slide_import, "problems": problems, "notes": notes}
    human = "\n".join((["OK"] if not problems else [f"problem: {p}" for p in problems]) + [f"note: {n}" for n in notes])
    _emit(doc, a.json, human)
    return 0 if not problems else 3


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slide-sync", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"slide-sync-skill {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--ffmpeg-skill", help="ffmpeg-skill directory (default: see README)")
        sp.add_argument("--timeout", type=float, default=3600.0, help="seconds per ffmpeg-skill call (default 3600)")
        sp.add_argument("--json", action="store_true", help="print one JSON document")

    d = sub.add_parser("detect", help="slide timeline of a recording")
    d.add_argument("video")
    d.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help=f"scdet cut score 0-100 (default {DEFAULT_THRESHOLD:g})")
    d.add_argument("--min-dwell", type=float, default=DEFAULT_MIN_DWELL, help=f"seconds a slide must stay (default {DEFAULT_MIN_DWELL:g})")
    d.add_argument("--sheet", help="also write a contact sheet PNG, one thumbnail per slide")
    d.add_argument("-o", "--output", help="also write the timeline JSON here")
    d.add_argument("--follow", action="store_true",
                   help="the recording is still being written: report slide changes as they are found (JSON lines with "
                        "--json) until the file has not changed for --settle seconds")
    d.add_argument("--settle", type=float, default=DEFAULT_SETTLE,
                   help=f"with --follow: seconds unchanged before the recording counts as closed (default {DEFAULT_SETTLE:g})")
    d.add_argument("--poll", type=float, default=DEFAULT_POLL,
                   help=f"with --follow: seconds between looks at the file (default {DEFAULT_POLL:g})")
    d.add_argument("--follow-for", type=float, help="with --follow: stop after this many seconds even if it still grows")
    d.add_argument("--chapters-out", help="also write chapter text (Slide N) here; with --follow it is updated at every change")
    d.add_argument("--overwrite", action="store_true")
    common(d)
    d.set_defaults(func=cmd_detect)

    c = sub.add_parser("chapters", help="chapters from a slide timeline")
    c.add_argument("video")
    c.add_argument("--timeline", help="timeline JSON from `detect -o` (default: detect now)")
    c.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    c.add_argument("--min-dwell", type=float, default=DEFAULT_MIN_DWELL)
    c.add_argument("--titles", help="text file, one chapter title per slide, in order")
    c.add_argument("--prefix", default="Slide", help="title for slides without one: '<prefix> N' (default Slide)")
    c.add_argument("--min-chapter", type=float, default=0.0, help="merge slides shorter than this into the previous chapter")
    c.add_argument("--chapters-out", help="write the chapter text (YouTube description format) here")
    c.add_argument("-o", "--output", help="write a copy of the video with container chapters (streams copied)")
    c.add_argument("--overwrite", action="store_true")
    common(c)
    c.set_defaults(func=cmd_chapters)

    im = sub.add_parser("import", help="page PNGs and page text of a slide deck (PDF; pptx/ppt/key/odp via LibreOffice)")
    im.add_argument("deck", help="a .pdf, or .pptx / .ppt / .key / .odp when LibreOffice is installed")
    im.add_argument("-o", "--output", required=True, help="directory for page-NNN.png, page-NNN.txt and deck.json")
    im.add_argument("--dpi", type=float, default=DEFAULT_DPI,
                    help=f"render resolution, {MIN_DPI:g}-{MAX_DPI:g} (default {DEFAULT_DPI:g}: a 16:9 PowerPoint slide is 1920x1080)")
    im.add_argument("--soffice", help="LibreOffice soffice binary (default: $SLIDE_SYNC_SOFFICE, PATH, the usual install places)")
    im.add_argument("--timeout", type=float, default=600.0, help="seconds for the LibreOffice conversion (default 600)")
    im.add_argument("--overwrite", action="store_true", help="replace page files and deck.json already in the directory")
    im.add_argument("--json", action="store_true", help="print one JSON document")
    im.set_defaults(func=cmd_import)

    m = sub.add_parser("match", help="which deck page is on screen in each slide of the timeline")
    m.add_argument("video")
    m.add_argument("--deck", required=True, help="directory written by `slide-sync import` (deck.json and page PNGs)")
    m.add_argument("--timeline", help="timeline JSON from `detect -o` (default: detect now)")
    m.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="for detect when there is no --timeline")
    m.add_argument("--min-dwell", type=float, default=DEFAULT_MIN_DWELL, help="for detect when there is no --timeline")
    m.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE,
                   help=f"lowest correlation that counts as a match, -1 to 1 (default {DEFAULT_MIN_SCORE:g})")
    m.add_argument("--min-margin", type=float, default=DEFAULT_MIN_MARGIN,
                   help=f"how far the best page must lead the runner-up (default {DEFAULT_MIN_MARGIN:g})")
    m.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                   help=f"frames compared per slide, 1-{MAX_SAMPLES}; the median score counts (default {DEFAULT_SAMPLES})")
    m.add_argument("-o", "--output", help="also write the match JSON here")
    m.add_argument("--overwrite", action="store_true")
    common(m)
    m.set_defaults(func=cmd_match)

    ins = sub.add_parser("insert", help="splice clips (a demo video, a still) in at slide boundaries; chapters shift with them")
    ins.add_argument("video")
    ins.add_argument("--insert", action="append", required=True, metavar="N=CLIP",
                     help="insert CLIP after slide N of the timeline (0: before slide 1); repeat for more. A video "
                          "clip goes in as it is, an image (png/jpg/...) becomes a still clip of --still-duration")
    ins.add_argument("--insert-title", action="append", default=[], metavar="TITLE",
                     help="chapter title of the inserted clips, in --insert order (default: the clip's file name)")
    ins.add_argument("--still-duration", type=float, default=DEFAULT_STILL_DURATION,
                     help=f"seconds an inserted image stays on screen (default {DEFAULT_STILL_DURATION:g})")
    ins.add_argument("--timeline", help="timeline JSON from `detect -o` (default: detect now)")
    ins.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="for detect when there is no --timeline")
    ins.add_argument("--min-dwell", type=float, default=DEFAULT_MIN_DWELL, help="for detect when there is no --timeline")
    ins.add_argument("--titles", help="text file, one chapter title per slide of the recording, in order")
    ins.add_argument("--prefix", default="Slide", help="title for slides without one: '<prefix> N' (default Slide)")
    ins.add_argument("--min-chapter", type=float, default=0.0, help="merge segments shorter than this into the previous chapter")
    ins.add_argument("--chapters-out", help="write the chapter text of the output (YouTube description format) here")
    ins.add_argument("--timeline-out", help="write the shifted timeline of the output (provenance DERIVED) here")
    ins.add_argument("-o", "--output", required=True, help="the new video: clips spliced in, chapters written")
    ins.add_argument("--overwrite", action="store_true")
    common(ins)
    ins.set_defaults(func=cmd_insert)

    lay = sub.add_parser("layout", help="a layout per segment (slide, slide + speaker PiP, side by side, speaker full) "
                                        "as ffmpeg-skill overlay / broll steps")
    lay.add_argument("video", help="the slide recording (screen capture or camera on the projection)")
    lay.add_argument("--match", help="match JSON from `slide-sync match -o`")
    lay.add_argument("--deck", help="or: an imported deck directory, to match now")
    lay.add_argument("--timeline", help="with --deck: timeline JSON from `detect -o` (default: detect now)")
    lay.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="for detect when there is no --timeline")
    lay.add_argument("--min-dwell", type=float, default=DEFAULT_MIN_DWELL, help="for detect when there is no --timeline")
    lay.add_argument("--camera", help="the speaker camera, on the recording's clock (see --camera-offset)")
    lay.add_argument("--camera-offset", type=float, default=0.0,
                     help="camera time minus recording time, in seconds (default 0: the two start together)")
    lay.add_argument("--qa-from", type=float, help="from this time of the recording on, the speaker full (Q&A)")
    lay.add_argument("--slides", choices=SLIDE_MODES, default="auto",
                     help="layout while a deck page is on screen: auto (side by side when the slide leaves a side band, "
                          "else PiP), pip, side, full (default auto)")
    lay.add_argument("--unmatched", choices=UNMATCHED_MODES, default="keep",
                     help="an unmatched segment between matched pages: keep the slide picture (default) or speaker full")
    lay.add_argument("--pip-position", choices=PIP_POSITIONS, default=DEFAULT_PIP_POSITION)
    lay.add_argument("--pip-width", type=float, default=DEFAULT_PIP_WIDTH,
                     help=f"PiP width, percent of the frame width (default {DEFAULT_PIP_WIDTH:g})")
    lay.add_argument("--plan-out", help="write the layout plan JSON here")
    lay.add_argument("-o", "--output", help="render the plan through ffmpeg-skill into this video")
    lay.add_argument("--overwrite", action="store_true")
    common(lay)
    lay.set_defaults(func=cmd_layout)

    cl = sub.add_parser("clean", help="the deck page, sharp, over the captured slide where match is confident")
    cl.add_argument("video", help="the slide recording (a camera on the projection, or a soft screen capture)")
    cl.add_argument("--match", help="match JSON from `slide-sync match -o`")
    cl.add_argument("--deck", help="the imported deck directory (default: the one the match names); without --match, "
                                   "match now with it")
    cl.add_argument("--timeline", help="without --match: timeline JSON from `detect -o` (default: detect now)")
    cl.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="for detect when there is no --timeline")
    cl.add_argument("--min-dwell", type=float, default=DEFAULT_MIN_DWELL, help="for detect when there is no --timeline")
    cl.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE,
                    help=f"replace only segments matched at least this confidently (default {DEFAULT_MIN_CONFIDENCE:g})")
    cl.add_argument("--pad-color", default="black", help="bars around a page of another aspect (default black)")
    cl.add_argument("--plan-out", help="write the plan JSON here")
    cl.add_argument("-o", "--output", help="render the plan through ffmpeg-skill into this video")
    cl.add_argument("--overwrite", action="store_true")
    common(cl)
    cl.set_defaults(func=cmd_clean)

    k = sub.add_parser("contract", help="machine-readable contract")
    k.add_argument("--json", action="store_true")
    k.set_defaults(func=cmd_contract)

    dr = sub.add_parser("doctor", help="environment check")
    dr.add_argument("--ffmpeg-skill")
    dr.add_argument("--soffice", help="LibreOffice soffice binary to check")
    dr.add_argument("--json", action="store_true")
    dr.set_defaults(func=cmd_doctor)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    a = parser().parse_args(argv)
    try:
        return a.func(a)
    except SlideSyncError as e:
        return _fail(e.code, str(e), getattr(a, "json", False))
    except EngineError as e:
        return _fail(e.code, str(e), getattr(a, "json", False), e.detail)


if __name__ == "__main__":
    sys.exit(main())
