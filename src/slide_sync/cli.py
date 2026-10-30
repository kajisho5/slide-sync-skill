"""slide-sync CLI. Every command prints one JSON document with --json.

  slide-sync detect talk.mp4 --json                      # slide timeline
  slide-sync detect talk.mp4 --sheet sheet.png -o timeline.json
  slide-sync chapters talk.mp4 --timeline timeline.json --chapters-out chapters.txt
  slide-sync chapters talk.mp4 --titles titles.txt --min-chapter 10 -o talk_chapters.mp4
  slide-sync import deck.pdf -o pages/ --json            # page PNGs + page text (pptx/key via LibreOffice)
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
from .core import DEFAULT_MIN_DWELL, DEFAULT_THRESHOLD, SlideSyncError, chapters, detect, read_titles, write_chapters
from .deck import DEFAULT_DPI, MAX_DPI, MIN_DPI, import_deck, pdfium_status, soffice_status
from .engine import REQUIRED_TOOLS, EngineError, FfmpegSkill, candidates, engine_doctor, locate


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


def cmd_detect(a: argparse.Namespace) -> int:
    skill = _engine(a.ffmpeg_skill)
    tl = detect(skill, a.video, threshold=a.threshold, min_dwell=a.min_dwell, sheet=a.sheet, timeout=a.timeout)
    if a.output:
        _write_json(a.output, tl, a.overwrite)
    lines = [f"{tl['slide_count']} slide(s) over {tl['source']['duration']:.1f}s"]
    lines += [f"  {s['index']:>3}  {s['start']:>9.3f}  {s['duration']:>8.3f}s" for s in tl["slides"]]
    _emit(dict(tl, ok=True, timeline_file=a.output), a.json, "\n".join(lines))
    return 0


def cmd_chapters(a: argparse.Namespace) -> int:
    skill = _engine(a.ffmpeg_skill)
    if a.timeline:
        try:
            with open(a.timeline, encoding="utf-8") as fh:
                tl = json.load(fh)
        except (OSError, ValueError) as e:
            raise SlideSyncError("INVALID_TIMELINE", f"cannot read {a.timeline}: {e}")
    else:
        tl = detect(skill, a.video, threshold=a.threshold, min_dwell=a.min_dwell, timeout=a.timeout)
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
        engine.update(ffmpeg=d["ffmpeg"], ffprobe=d["ffprobe"], tools=d["tools"])
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
