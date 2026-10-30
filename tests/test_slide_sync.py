"""slide-sync-skill tests.

Unit tests fake ffmpeg-skill's answers. Integration tests run the real ffmpeg-skill on generated slide decks;
they are skipped unless ffmpeg-skill is found (FFMPEG_SKILL_DIR, ../ffmpeg-skill, ...) and ffmpeg is on PATH.
Import tests build their PDF / PPTX decks here at test time; they need pypdfium2 (`pip install -e '.[pdf]'`), and the
real-LibreOffice test also needs soffice. Match unit tests draw their pages and frames in Python; the shuffled-slides
integration test imports a generated PDF, films it with ffmpeg in shuffled order (screen capture and keystoned camera)
and needs ffmpeg-skill, ffmpeg and pypdfium2. Insert unit tests shift timelines and fake ffmpeg-skill; the insert
integration test splices generated clips into a generated talk with the real ffmpeg-skill.

    python -m unittest -v tests/test_slide_sync.py
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slide_sync import __version__  # noqa: E402
from slide_sync import cli  # noqa: E402
from slide_sync.contract import ERRORS, contract  # noqa: E402
from slide_sync.core import TIMELINE_SCHEMA, SlideSyncError, chapters, detect, timecode, write_chapters  # noqa: E402
from slide_sync import deck as deck_mod  # noqa: E402
from slide_sync.deck import DECK_SCHEMA, convert_to_pdf, find_soffice, import_deck, normalise_text  # noqa: E402
from slide_sync.engine import INSERT_TOOLS, EngineError, FfmpegSkill, locate, run_tool  # noqa: E402
from slide_sync.insert import insert_clips, parse_insert, segment_titles, shift_timeline  # noqa: E402
from slide_sync import match as match_mod  # noqa: E402
from slide_sync.match import MATCH_SCHEMA, decide, match, quad_sampler, read_png, sample_times, slide_quad  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    def __init__(self, answers):
        self.answers = answers  # tool name -> (rc, stdout dict or str, stderr)
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        tool = Path(argv[1]).stem
        rc, out, err = self.answers[tool]
        return rc, out if isinstance(out, str) else json.dumps(out), err


SKILL = FfmpegSkill("/fake/ffmpeg-skill", "2.21.0", ["look", "metadata", "probe", "scenes"])


def scenes_doc(bounds, duration):
    return {"status": "completed", "duration": duration,
            "scenes": [{"index": i, "start": s, "end": e, "duration": e - s} for i, (s, e) in enumerate(bounds)]}


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, "talk.mp4")
        Path(self.video).write_bytes(b"x")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_timeline_from_scenes(self):
        r = FakeRunner({"scenes": (0, scenes_doc([(0, 4), (4, 9.5), (9.5, 20)], 20.0), "")})
        tl = detect(SKILL, self.video, threshold=6, min_dwell=3, runner=r)
        self.assertEqual(tl["schema"], TIMELINE_SCHEMA)
        self.assertEqual(tl["slide_count"], 3)
        self.assertEqual(tl["changes"], [4.0, 9.5])
        self.assertEqual(tl["slides"][1], {"index": 2, "start": 4.0, "end": 9.5, "duration": 5.5})
        self.assertEqual((tl["detector"]["threshold"], tl["detector"]["min_dwell"], tl["detector"]["engine_version"]), (6, 3, "2.21.0"))
        argv = r.calls[0]
        self.assertEqual(Path(argv[1]).name, "scenes.py")
        self.assertIn("--min-scene", argv)
        self.assertEqual(argv[argv.index("--min-scene") + 1], "3")
        self.assertEqual(argv[-1], "--json")

    def test_missing_input_and_bad_arguments(self):
        with self.assertRaises(SlideSyncError) as e:
            detect(SKILL, os.path.join(self.tmp, "nope.mp4"), runner=FakeRunner({}))
        self.assertEqual(e.exception.code, "INPUT_NOT_FOUND")
        for kw in ({"threshold": 0}, {"threshold": 101}, {"min_dwell": 0}):
            with self.assertRaises(SlideSyncError) as e:
                detect(SKILL, self.video, runner=FakeRunner({}), **kw)
            self.assertEqual(e.exception.code, "INVALID_ARGUMENT")

    def test_engine_failure_carries_its_message(self):
        r = FakeRunner({"scenes": (1, "", "progress\nerror: cannot decode input")})
        with self.assertRaises(EngineError) as e:
            detect(SKILL, self.video, runner=r)
        self.assertEqual((e.exception.code, e.exception.detail), ("ENGINE_FAILED", "error: cannot decode input"))

    def test_engine_answer_that_is_not_json(self):
        with self.assertRaises(EngineError) as e:
            run_tool(SKILL, "scenes", ["x"], runner=FakeRunner({"scenes": (0, "not json", "")}))
        self.assertEqual(e.exception.code, "ENGINE_FAILED")

    def test_engine_without_the_tool(self):
        with self.assertRaises(EngineError) as e:
            run_tool(FfmpegSkill("/x", "1.0.0", ["probe"]), "scenes", [], runner=FakeRunner({}))
        self.assertEqual(e.exception.code, "ENGINE_TOOL_MISSING")


def timeline(durations):
    slides, t = [], 0.0
    for i, d in enumerate(durations):
        slides.append({"index": i + 1, "start": t, "end": t + d, "duration": d})
        t += d
    return {"schema": TIMELINE_SCHEMA, "slides": slides}


class ChapterTests(unittest.TestCase):
    def test_one_chapter_per_slide_with_titles_then_prefix(self):
        ch = chapters(timeline([30, 40, 50]), titles=["はじめに", "方法"], prefix="Slide")
        self.assertEqual([c["title"] for c in ch["chapters"]], ["はじめに", "方法", "Slide 3"])
        self.assertEqual(ch["text"], "0:00 はじめに\n0:30 方法\n1:10 Slide 3\n")
        self.assertTrue(any("title(s) for 3 slide(s)" in w for w in ch["warnings"]))

    def test_short_slides_merge_into_the_previous_chapter(self):
        ch = chapters(timeline([30, 3, 2, 40, 5]), min_chapter=10)
        self.assertEqual([(c["start"], c["end"], c["slides"]) for c in ch["chapters"]], [(0.0, 35.0, [1, 2, 3]), (35.0, 80.0, [4, 5])])

    def test_youtube_rules_are_reported(self):
        ch = chapters(timeline([5, 5]))
        self.assertEqual(len(ch["warnings"]), 2)
        self.assertEqual(chapters(timeline([20, 30, 40]))["warnings"], [])

    def test_first_chapter_starts_at_zero_and_hours_format(self):
        tl = timeline([3600, 125])
        tl["slides"][0]["start"] = 0.4
        ch = chapters(tl)
        self.assertEqual(ch["chapters"][0]["start"], 0.0)
        self.assertEqual(timecode(3725), "1:02:05")

    def test_rejects_other_documents(self):
        with self.assertRaises(SlideSyncError) as e:
            chapters({"schema": "x", "slides": []})
        self.assertEqual(e.exception.code, "INVALID_TIMELINE")

    def test_write_never_targets_the_input_and_passes_chapters_to_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, "a.mp4")
            Path(video).write_bytes(b"x")
            ch = chapters(timeline([20, 30, 40]), titles=["A", "B", "C"])
            with self.assertRaises(SlideSyncError) as e:
                write_chapters(SKILL, video, ch, video, runner=FakeRunner({}))
            self.assertEqual(e.exception.code, "OUTPUT_IS_INPUT")
            seen = {}

            def runner(argv, timeout):
                seen["argv"] = argv
                seen["chapters"] = Path(argv[argv.index("--chapters") + 1]).read_text(encoding="utf-8")
                return 0, json.dumps({"status": "completed", "output": argv[argv.index("-o") + 1], "commands": []}), ""
            out = write_chapters(SKILL, video, ch, os.path.join(tmp, "b.mp4"), runner=runner)
            self.assertEqual(Path(seen["argv"][1]).name, "metadata.py")
            self.assertNotIn("--overwrite", seen["argv"])
            self.assertEqual(seen["chapters"], "0.000 A\n20.000 B\n50.000 C\n")
            self.assertTrue(out["output"].endswith("b.mp4"))


class ShiftTimelineTests(unittest.TestCase):
    """Timeline shifting for `insert`: pure arithmetic on a timeline, no engine."""

    def tl(self, durations=(30, 40, 50)):
        t = timeline(list(durations))
        t.update(source={"path": "/rec/talk.mp4", "duration": float(sum(durations))}, provenance="OBSERVED",
                 detector={"engine": "ffmpeg-skill/scenes"})
        return t

    def test_later_slides_shift_by_the_inserted_duration(self):
        d = shift_timeline(self.tl(), [{"after_slide": 1, "duration": 12.5, "clip": "/clips/demo.mp4"}], output="/out/x.mp4")
        got = [(s["index"], s["kind"], s.get("source_slide"), s["start"], s["end"], s["duration"]) for s in d["slides"]]
        self.assertEqual(got, [(1, "slide", 1, 0.0, 30.0, 30.0), (2, "insert", None, 30.0, 42.5, 12.5),
                               (3, "slide", 2, 42.5, 82.5, 40.0), (4, "slide", 3, 82.5, 132.5, 50.0)])
        self.assertEqual(d["changes"], [30.0, 42.5, 82.5])
        self.assertEqual((d["slide_count"], d["inserted_count"]), (4, 1))
        self.assertEqual(d["source"], {"path": os.path.abspath("/out/x.mp4"), "duration": 132.5})
        self.assertEqual(d["slides"][1]["title"], "demo")  # the clip's file name when no title is given

    def test_provenance_records_every_edit(self):
        d = shift_timeline(self.tl(), [{"after_slide": 2, "duration": 5, "clip": "/c/a.png", "kind": "still",
                                        "sha256": "ab" * 32, "title": "Card"}])
        self.assertEqual(d["provenance"], "DERIVED")
        self.assertEqual(d["derived_from"], {"path": "/rec/talk.mp4", "duration": 120.0, "provenance": "OBSERVED", "slide_count": 3})
        self.assertEqual(d["detector"], {"engine": "ffmpeg-skill/scenes"})
        self.assertEqual(d["edits"], [{"op": "insert", "insert": 1, "after_slide": 2, "at_source": 70.0, "at_output": 70.0,
                                       "duration": 5.0, "shift_after": 5.0, "title": "Card",
                                       "clip": os.path.abspath("/c/a.png"), "clip_kind": "still", "clip_sha256": "ab" * 32}])

    def test_several_inserts_at_the_start_the_end_and_one_boundary(self):
        ins = [{"after_slide": 3, "duration": 4, "title": "End"}, {"after_slide": 1, "duration": 2, "title": "A"},
               {"after_slide": 0, "duration": 3, "title": "Intro"}, {"after_slide": 1, "duration": 1, "title": "B"}]
        d = shift_timeline(self.tl(), ins)
        self.assertEqual([(s.get("title") or s["source_slide"], s["start"], s["end"]) for s in d["slides"]],
                         [("Intro", 0.0, 3.0), (1, 3.0, 33.0), ("A", 33.0, 35.0), ("B", 35.0, 36.0), (2, 36.0, 76.0),
                          (3, 76.0, 126.0), ("End", 126.0, 130.0)])
        self.assertEqual([e["insert"] for e in d["edits"]], [3, 2, 4, 1])  # placed in timeline order, given order kept
        self.assertEqual(d["source"]["duration"], 130.0)

    def test_shifting_a_shifted_timeline_keeps_the_source_slides(self):
        once = shift_timeline(self.tl(), [{"after_slide": 1, "duration": 10, "title": "Demo"}])
        twice = shift_timeline(once, [{"after_slide": 3, "duration": 5, "title": "Card"}])
        self.assertEqual([(s["kind"], s.get("source_slide"), s["start"]) for s in twice["slides"]],
                         [("slide", 1, 0.0), ("insert", None, 30.0), ("slide", 2, 40.0), ("insert", None, 80.0),
                          ("slide", 3, 85.0)])
        self.assertEqual([e["title"] for e in twice["edits"]], ["Demo", "Card"])

    def test_chapters_follow_the_shift_and_keep_slide_titles(self):
        d = shift_timeline(self.tl(), [{"after_slide": 2, "duration": 15, "title": "デモ"}])
        names = segment_titles(d, ["はじめに", "方法"], prefix="Slide")
        self.assertEqual(names, ["はじめに", "方法", "デモ", "Slide 3"])
        ch = chapters(d, titles=names)
        self.assertEqual(ch["text"], "0:00 はじめに\n0:30 方法\n1:10 デモ\n1:25 Slide 3\n")
        self.assertEqual(ch["warnings"], [])
        merged = chapters(shift_timeline(self.tl(), [{"after_slide": 2, "duration": 4, "title": "x"}]), min_chapter=10)
        self.assertEqual([(c["start"], c["end"]) for c in merged["chapters"]], [(0.0, 30.0), (30.0, 74.0), (74.0, 124.0)])

    def test_bad_inserts(self):
        for ins, code in (([{"after_slide": 4, "duration": 1}], "INVALID_ARGUMENT"),
                          ([{"after_slide": -1, "duration": 1}], "INVALID_ARGUMENT"),
                          ([{"after_slide": 1, "duration": 0}], "INVALID_ARGUMENT"),
                          ([{"after_slide": True, "duration": 1}], "INVALID_ARGUMENT")):
            with self.assertRaises(SlideSyncError) as e:
                shift_timeline(self.tl(), ins)
            self.assertEqual(e.exception.code, code)
        with self.assertRaises(SlideSyncError) as e:
            shift_timeline({"schema": "x"}, [])
        self.assertEqual(e.exception.code, "INVALID_TIMELINE")

    def test_parse_insert(self):
        self.assertEqual(parse_insert("3=demo.mp4"), (3, "demo.mp4"))
        self.assertEqual(parse_insert("0=C:\\clips\\a=b.mp4"), (0, "C:\\clips\\a=b.mp4"))
        for bad in ("demo.mp4", "x=demo.mp4", "-1=a.mp4", "2="):
            with self.assertRaises(SlideSyncError):
                parse_insert(bad)


class InsertRunner:
    """Fake ffmpeg-skill for `insert`: answers probe / insert / cut / join / metadata and records every call."""

    def __init__(self, durations, video=(1920, 1080, 30.0), join_duration=None):
        self.durations = durations  # basename -> seconds
        self.video = video
        self.join_duration = join_duration
        self.calls = []
        self.chapters = None

    def probe(self, path, duration):
        w, h, fps = self.video
        return {"file": path, "duration": duration, "video": {"width": w, "height": h, "fps": fps}}

    def __call__(self, argv, timeout):
        tool, args = Path(argv[1]).stem, argv[2:]
        self.calls.append((tool, args))
        out = args[args.index("-o") + 1] if "-o" in args else None
        if tool == "probe":
            return 0, json.dumps(self.probe(args[0], self.durations[os.path.basename(args[0])])), ""
        if tool == "insert":
            return 0, json.dumps({"status": "completed", "output": out,
                                  "probe": self.probe(out, float(args[args.index("--duration") + 1]))}), ""
        if tool == "cut":
            start = float(args[args.index("--start") + 1])
            end = float(args[args.index("--end") + 1]) if "--end" in args else self.durations[os.path.basename(args[0])]
            return 0, json.dumps({"status": "completed", "output": out, "mode": "accurate", "output_duration": end - start,
                                  "probe": self.probe(out, end - start)}), ""
        if tool == "join":
            total = self.join_duration
            return 0, json.dumps({"status": "completed", "output": out, "probe": self.probe(out, total)}), ""
        if tool == "metadata":
            self.chapters = Path(args[args.index("--chapters") + 1]).read_text(encoding="utf-8")
            return 0, json.dumps({"status": "completed", "output": out, "commands": []}), ""
        raise AssertionError(tool)


class InsertOpTests(unittest.TestCase):
    SKILL = FfmpegSkill("/fake/ffmpeg-skill", "2.21.0", sorted(set(INSERT_TOOLS) | {"look", "scenes"}))

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.video = os.path.join(self.tmp, "talk.mp4")
        self.demo = os.path.join(self.tmp, "demo.mp4")
        self.page = os.path.join(self.tmp, "page-004.png")
        for f in (self.video, self.demo, self.page):
            Path(f).write_bytes(b"x")
        self.tl = timeline([30, 40, 50])
        self.tl.update(source={"path": self.video, "duration": 120.0}, provenance="OBSERVED")
        self.out = os.path.join(self.tmp, "out.mp4")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_cut_insert_join_metadata_in_order(self):
        r = InsertRunner({"talk.mp4": 120.0, "demo.mp4": 12.5}, join_duration=137.5)
        res = insert_clips(self.SKILL, self.video, self.tl, [(1, self.demo), (2, self.page)], self.out,
                           titles=["A", "B", "C"], insert_titles=["Demo"], still_duration=5, runner=r)
        tools = [t for t, _ in r.calls]
        self.assertEqual(tools, ["probe", "probe", "insert", "cut", "cut", "cut", "join", "metadata"])
        ins = dict(r.calls)["insert"]
        self.assertEqual(ins[:9], [self.page, "--duration", "5", "--width", "1920", "--height", "1080", "--fps", "30"])
        cuts = [a for t, a in r.calls if t == "cut"]
        self.assertEqual([(a[a.index("--start") + 1], a[a.index("--end") + 1] if "--end" in a else None) for a in cuts],
                         [("0.000", "30.000"), ("30.000", "70.000"), ("70.000", None)])
        self.assertTrue(all("--accurate" in a for a in cuts))
        join = dict(r.calls)["join"]
        names = [os.path.basename(x) for x in join[:join.index("--transition")]]
        self.assertEqual(names, ["part-001.mp4", "demo.mp4", "part-002.mp4", "insert-002.mp4", "part-003.mp4"])
        self.assertEqual(join[join.index("--transition"):join.index("-o")],
                         ["--transition", "none", "--width", "1920", "--height", "1080", "--fps", "30"])
        self.assertEqual(r.chapters, "0.000 A\n30.000 Demo\n42.500 B\n82.500 page-004\n87.500 C\n")
        self.assertEqual(res["text"], "0:00 A\n0:30 Demo\n0:42 B\n1:22 page-004\n1:28 C\n")
        tl = res["timeline"]
        self.assertEqual((tl["provenance"], tl["source"]["duration"], tl["render"]["drift"]), ("DERIVED", 137.5, 0.0))
        self.assertEqual([(e["after_slide"], e["clip_kind"], e["duration"]) for e in tl["edits"]],
                         [(1, "video", 12.5), (2, "still", 5.0)])
        self.assertEqual(tl["edits"][0]["clip_sha256"], hashlib.sha256(b"x").hexdigest())
        self.assertEqual([s["tool"] for s in tl["render"]["steps"]], tools)
        self.assertEqual(res["output"], os.path.abspath(self.out))

    def test_clips_at_the_ends_need_no_cut(self):
        r = InsertRunner({"talk.mp4": 120.0, "demo.mp4": 3.0}, join_duration=126.0)
        res = insert_clips(self.SKILL, self.video, self.tl, [(3, self.demo), (0, self.demo)], self.out, runner=r)
        self.assertNotIn("cut", [t for t, _ in r.calls])
        join = dict(r.calls)["join"]
        self.assertEqual([os.path.basename(x) for x in join[:3]], ["demo.mp4", "talk.mp4", "demo.mp4"])
        self.assertEqual([s["start"] for s in res["timeline"]["slides"]], [0.0, 3.0, 33.0, 73.0, 123.0])

    def test_drift_is_measured_and_warned(self):
        r = InsertRunner({"talk.mp4": 120.0, "demo.mp4": 12.5}, join_duration=133.0)
        res = insert_clips(self.SKILL, self.video, self.tl, [(1, self.demo)], self.out, runner=r)
        self.assertEqual(res["timeline"]["render"]["drift"], 0.5)
        self.assertTrue(any("off the predicted length" in w for w in res["warnings"]))

    def test_refusals(self):
        r = InsertRunner({"talk.mp4": 120.0, "demo.mp4": 1.0}, join_duration=121.0)
        cases = [((self.tl, [(4, self.demo)], self.out), {}, "INVALID_ARGUMENT"),
                 ((self.tl, [(1, os.path.join(self.tmp, "nope.mp4"))], self.out), {}, "INPUT_NOT_FOUND"),
                 ((self.tl, [(1, self.demo)], self.video), {}, "OUTPUT_IS_INPUT"),
                 ((self.tl, [(1, self.demo)], self.demo), {}, "OUTPUT_IS_INPUT"),
                 ((self.tl, [], self.out), {}, "INVALID_ARGUMENT"),
                 ((self.tl, [(1, self.demo)], self.out), {"still_duration": 0}, "INVALID_ARGUMENT"),
                 (({"schema": "x"}, [(1, self.demo)], self.out), {}, "INVALID_TIMELINE")]
        for args, kw, code in cases:
            with self.assertRaises(SlideSyncError) as e:
                insert_clips(self.SKILL, self.video, *args, runner=r, **kw)
            self.assertEqual(e.exception.code, code, (args, kw))
        Path(self.out).write_bytes(b"old")
        with self.assertRaises(SlideSyncError) as e:
            insert_clips(self.SKILL, self.video, self.tl, [(1, self.demo)], self.out, runner=r)
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(r.calls, [])  # nothing ran for any refusal
        with self.assertRaises(EngineError) as e:
            insert_clips(FfmpegSkill("/x", "2.0.0", ["scenes", "metadata", "look"]), self.video, self.tl, [(1, self.demo)],
                         os.path.join(self.tmp, "o2.mp4"), runner=r)
        self.assertEqual(e.exception.code, "ENGINE_TOOL_MISSING")

    def test_cli(self):
        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch.object(cli, "_engine", return_value=self.SKILL):
                rc = cli.main(list(argv) + ["--json"])  # every case is refused before ffmpeg-skill runs
            return rc, json.loads(buf.getvalue())
        tl = os.path.join(self.tmp, "tl.json")
        Path(tl).write_text(json.dumps(self.tl), encoding="utf-8")
        rc, doc = run("insert", self.video, "--timeline", tl, "--insert", "demo.mp4", "-o", self.out)
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        rc, doc = run("insert", self.video, "--timeline", tl, "--insert", f"9={self.demo}", "-o", self.out)
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        Path(self.tmp, "exists.json").write_text("{}", encoding="utf-8")
        rc, doc = run("insert", self.video, "--timeline", tl, "--insert", f"1={self.demo}", "-o", self.out,
                      "--timeline-out", os.path.join(self.tmp, "exists.json"))
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))


class ContractTests(unittest.TestCase):
    def test_os_registry_keys_and_provides(self):
        c = contract()
        for k in ("skill_id", "id", "skill", "version", "contract_version", "provides", "dependencies", "not_provided"):
            self.assertIn(k, c)
        self.assertEqual(c["version"], __version__)
        self.assertEqual({p["tool_id"] for p in c["provides"]}, {t["tool_id"] for t in c["tools"]})
        self.assertIn("slides.import", {p["id"] for p in c["provides"]})
        self.assertIn("slides.match", {p["id"] for p in c["provides"]})
        self.assertIn("slides.insert_clips", {p["id"] for p in c["provides"]})
        self.assertEqual(c["insert"]["provenance"], "DERIVED")
        self.assertTrue({"cut", "join", "insert", "probe"} <= set(next(d for d in c["dependencies"] if d["id"] == "ffmpeg-skill")["tools"]))
        self.assertIn("look", next(d for d in c["dependencies"] if d["id"] == "ffmpeg-skill")["tools"])
        self.assertEqual(c["match"]["schema"], MATCH_SCHEMA)
        optional = {d["id"]: d for d in c["dependencies"] if not d["required"]}
        self.assertEqual(set(optional), {"pypdfium2", "libreoffice"})
        self.assertNotIn("AGPL", json.dumps(c))
        for p in c["provides"]:
            self.assertEqual(set(p), {"id", "lifecycle", "tool_id"})
        self.assertFalse(c["execution"]["network"])

    def test_version_comes_from_pyproject(self):
        import re
        want = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
        self.assertEqual(__version__, want)

    def test_cli_contract_and_error_exit_codes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["contract", "--json"]), 0)
        self.assertEqual(json.loads(buf.getvalue())["skill_id"], "slide-sync")
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(buf):
            rc = cli.main(["detect", os.path.join(tmp, "missing.mp4"), "--ffmpeg-skill", tmp, "--json"])
        doc = json.loads(buf.getvalue())
        if doc["error"]["code"] == "ENGINE_NOT_FOUND":
            self.assertEqual(rc, ERRORS["ENGINE_NOT_FOUND"]["exit_code"])
        else:
            self.assertEqual((doc["error"]["code"], rc), ("INPUT_NOT_FOUND", 2))

    def test_no_ffmpeg_or_shell_in_the_skill_itself(self):
        import re
        launch = re.compile(r'(?<![\w\])])\[\s*["\']ff(mpeg|probe)["\']|which\(\s*["\']ff(mpeg|probe)')
        for f in (ROOT / "src" / "slide_sync").glob("*.py"):
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", text, f.name)
            self.assertIsNone(launch.search(text), f"{f.name} launches ffmpeg/ffprobe itself")


# ------------------------------------------------------------------ slide import fixtures (built at test time)

def build_pdf(pages, size=(960, 540)):
    """A small PDF: each page is (background rgb, [(font, text), ...]). 'latin' lines use Helvetica; 'cjk' lines use a
    Type0 / Identity-H font with a ToUnicode CMap, the way an exported deck's embedded subset font carries Japanese
    (the font itself is not embedded: the tests read the text layer, and check the picture by its background)."""
    w, h = size
    chars = sorted({c for _, lines in pages for font, text in lines if font == "cjk" for c in text})
    cid = {c: i + 1 for i, c in enumerate(chars)}
    cmap = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /Fixture def /CMapType 2 def\n"
            "1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            f"{len(chars)} beginbfchar\n" + "".join(f"<{cid[c]:04X}> <{ord(c):04X}>\n" for c in chars)
            + "endbfchar endcmap CMapName currentdict /CMap defineresource pop end end").encode("ascii")
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        4: b"<< /Type /Font /Subtype /Type0 /BaseFont /FixtureGothic /Encoding /Identity-H /DescendantFonts [6 0 R] /ToUnicode 5 0 R >>",
        5: b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream",
        6: b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /FixtureGothic "
           b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 1000 >>",
    }
    kids, n = [], 7
    for rgb, lines in pages:
        ops = ["%.4f %.4f %.4f rg 0 0 %d %d re f 0 0 0 rg" % (rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, w, h)]
        for k, (font, text) in enumerate(lines):
            y = h - 100 - 60 * k
            if font == "latin":
                ops.append("BT /F1 32 Tf 60 %d Td (%s) Tj ET" % (y, text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")))
            else:
                ops.append("BT /F2 32 Tf 60 %d Td <%s> Tj ET" % (y, "".join(f"{cid[c]:04X}" for c in text)))
        content = "\n".join(ops).encode("latin-1")
        objs[n] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        objs[n + 1] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
                       b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>" % (w, h, n))
        kids.append(n + 1)
        n += 2
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for i in sorted(objs):
        offsets[i] = len(out)
        out += b"%d 0 obj\n" % i + objs[i] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % n + b"".join(b"%010d 00000 n \n" % offsets[i] for i in range(1, n))
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (n, xref)
    return bytes(out)


_P = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')


def build_pptx(path, slides):
    """A minimal 16:9 .pptx (one text box per slide, one paragraph per line) that LibreOffice opens."""
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ct = "application/vnd.openxmlformats-officedocument.presentationml"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   f'<Override PartName="/ppt/presentation.xml" ContentType="{ct}.presentation.main+xml"/>'
                   + "".join(f'<Override PartName="/ppt/slides/slide{i + 1}.xml" ContentType="{ct}.slide+xml"/>'
                             for i in range(len(slides))) + "</Types>")
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships '
                   'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
                   f'Type="{rel}/officeDocument" Target="ppt/presentation.xml"/></Relationships>')
        z.writestr("ppt/_rels/presentation.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships '
                   'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Type="{rel}/slide" Target="slides/slide{i + 1}.xml"/>'
                             for i in range(len(slides))) + "</Relationships>")
        z.writestr("ppt/presentation.xml", f'<?xml version="1.0" encoding="UTF-8"?><p:presentation {_P}><p:sldIdLst>'
                   + "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(len(slides)))
                   + '</p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>')
        for i, lines in enumerate(slides):
            paras = "".join(f'<a:p><a:r><a:rPr lang="ja-JP" sz="3200"/><a:t>{escape(t)}</a:t></a:r></a:p>' for t in lines)
            z.writestr(f"ppt/slides/slide{i + 1}.xml",
                       f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld {_P}><p:cSld><p:spTree>'
                       '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
                       '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Text"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr>'
                       '<a:xfrm><a:off x="457200" y="457200"/><a:ext cx="11277600" cy="5943600"/></a:xfrm>'
                       '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
                       f"<p:txBody><a:bodyPr/><a:lstStyle/>{paras}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>")


def png_info(path):
    """(width, height, colour type, first pixel) of a PNG this skill wrote (filter 0 rows)."""
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    colour = data[25]
    idat, pos = b"", 8
    while pos < len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        if data[pos + 4:pos + 8] == b"IDAT":
            idat += data[pos + 8:pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    return width, height, colour, tuple(raw[1:4])


# The conference deck: English title page, a Japanese page (with U+2F47 KANGXI RADICAL SUN where 日 should be, as some
# PowerPoint / Keynote exports write it), and a mixed page.
DECK_PAGES = [
    ((255, 255, 255), [("latin", "Slide Sync: Talk Recordings"), ("latin", "A. Author (Example Univ.)")]),
    ((200, 30, 30), [("cjk", "研究の背景と目的"), ("cjk", "⽇本語の学会スライド")]),
    ((30, 60, 200), [("latin", "Results"), ("cjk", "精度は９５％、処理時間は２分")]),
]
DECK_TEXT = ["Slide Sync: Talk Recordings\nA. Author (Example Univ.)",
             "研究の背景と目的\n日本語の学会スライド",
             "Results\n精度は９５％、処理時間は２分"]


def _has_pdfium():
    try:
        import pypdfium2  # noqa: F401
        return True
    except ImportError:
        return False


HAS_PDFIUM = _has_pdfium()


class ImportWithoutDependenciesTests(unittest.TestCase):
    """What runs with or without the optional extra: arguments, the dependency errors, the LibreOffice call."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv) + ["--json"])
        return rc, json.loads(buf.getvalue())

    def test_text_normalisation(self):
        self.assertEqual(normalise_text("⽇本\r\nがくかい \r\n\r\n"), "日本\nがくかい")
        self.assertEqual(normalise_text("Ａ\x02Ｂ￾ ｶﾅ\r"), "ＡＢ ｶﾅ")  # full-width and half-width forms are kept

    def test_missing_pypdfium2_is_a_dependency_error(self):
        pdf = os.path.join(self.tmp, "deck.pdf")
        Path(pdf).write_bytes(build_pdf(DECK_PAGES[:1]))
        with mock.patch.dict(sys.modules, {"pypdfium2": None}):
            rc, doc = self.cli("import", pdf, "-o", os.path.join(self.tmp, "out"))
        self.assertEqual((rc, doc["error"]["code"]), (3, "DEPENDENCY_MISSING"))
        self.assertIn("slide-sync-skill[pdf]", doc["error"]["message"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out")))

    def test_missing_libreoffice_is_a_dependency_error(self):
        pptx = os.path.join(self.tmp, "deck.pptx")
        build_pptx(pptx, [["x"]])
        with mock.patch.object(deck_mod, "find_soffice", return_value=None), mock.patch.object(deck_mod, "_pdfium"):
            rc, doc = self.cli("import", pptx, "-o", os.path.join(self.tmp, "out"))
        self.assertEqual((rc, doc["error"]["code"]), (3, "DEPENDENCY_MISSING"))
        self.assertIn("LibreOffice", doc["error"]["message"])
        self.assertIn("export the deck as PDF", doc["error"]["message"])

    def test_bad_arguments(self):
        doc_path = os.path.join(self.tmp, "deck.docx")
        Path(doc_path).write_bytes(b"x")
        pdf = os.path.join(self.tmp, "deck.pdf")
        Path(pdf).write_bytes(b"%PDF-1.7\n")
        for argv in (["import", os.path.join(self.tmp, "none.pdf"), "-o", self.tmp],
                     ["import", doc_path, "-o", self.tmp], ["import", pdf, "-o", self.tmp, "--dpi", "1000"],
                     ["import", pdf, "-o", pdf]):
            rc, doc = self.cli(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn(doc["error"]["code"], ("INPUT_NOT_FOUND", "INVALID_ARGUMENT"), argv)

    def test_libreoffice_runs_headless_on_a_copy_with_its_own_profile(self):
        pptx = os.path.join(self.tmp, "my talk.pptx")
        build_pptx(pptx, [["x"]])
        work = tempfile.mkdtemp(dir=self.tmp)
        before = sorted(os.listdir(self.tmp))
        seen = {}

        def runner(argv, timeout):
            seen["argv"] = argv
            outdir = argv[argv.index("--outdir") + 1]
            Path(outdir, Path(argv[-1]).stem + ".pdf").write_bytes(b"%PDF")
            return 0, "", ""
        pdf, _ = convert_to_pdf(pptx, work, soffice=sys.executable, runner=runner)
        argv = seen["argv"]
        self.assertEqual(argv[0], os.path.realpath(sys.executable))
        self.assertIn("--headless", argv)
        self.assertEqual(argv[argv.index("--convert-to") + 1], "pdf")
        self.assertTrue(any(a.startswith("-env:UserInstallation=file:") for a in argv))
        self.assertNotEqual(argv[-1], pptx)  # a copy inside the work directory, never the deck itself
        self.assertTrue(argv[-1].startswith(work))
        self.assertTrue(os.path.isfile(pdf))
        self.assertEqual(sorted(os.listdir(self.tmp)), before)  # nothing written next to the deck

        def failing(argv, timeout):
            return 1, "", "Error: source file could not be loaded"
        with self.assertRaises(SlideSyncError) as e:
            convert_to_pdf(pptx, tempfile.mkdtemp(dir=self.tmp), soffice=sys.executable, runner=failing)
        self.assertEqual(e.exception.code, "CONVERT_FAILED")
        self.assertIn("could not be loaded", str(e.exception))

        def slow(argv, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)
        with self.assertRaises(SlideSyncError) as e:
            convert_to_pdf(pptx, tempfile.mkdtemp(dir=self.tmp), soffice=sys.executable, runner=slow)
        self.assertEqual((e.exception.code, ERRORS["CONVERT_TIMEOUT"]["retryable"]), ("CONVERT_TIMEOUT", True))

    def test_doctor_reports_the_import_dependencies(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["doctor", "--json", "--ffmpeg-skill", self.tmp])
        doc = json.loads(buf.getvalue())
        imp = doc["import"]
        self.assertEqual(imp["pdf"]["found"], HAS_PDFIUM)
        self.assertEqual(imp["ready"]["pdf"], HAS_PDFIUM)
        self.assertIn("found", imp["libreoffice"])
        self.assertEqual(imp["ready"]["pptx_key_odp"], HAS_PDFIUM and imp["libreoffice"]["found"])
        if not HAS_PDFIUM:
            self.assertTrue(any("slide-sync-skill[pdf]" in n for n in doc["notes"]))
        self.assertFalse(any("pypdfium2" in p or "LibreOffice" in p for p in doc["problems"]))


@unittest.skipUnless(HAS_PDFIUM, "needs pypdfium2")
class ImportTests(unittest.TestCase):
    """The done-when fixture of roadmap step 2: page count and the text of every page, Japanese included."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pdf = os.path.join(self.tmp, "talk deck.pdf")
        Path(self.pdf).write_bytes(build_pdf(DECK_PAGES))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_page_count_and_text_per_page(self):
        out = os.path.join(self.tmp, "pages")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["import", self.pdf, "-o", out, "--json"])
        doc = json.loads(buf.getvalue())
        self.assertEqual((rc, doc["ok"], doc["schema"], doc["page_count"]), (0, True, DECK_SCHEMA, 3))
        self.assertEqual([p["text"] for p in doc["pages"]], DECK_TEXT)
        for p, want in zip(doc["pages"], DECK_TEXT):
            self.assertEqual(Path(out, p["text_file"]).read_bytes(), (want + "\n").encode("utf-8"))
            self.assertEqual(p["chars"], len(want))
        self.assertEqual(sorted(os.listdir(out)), ["deck.json", "page-001.png", "page-001.txt", "page-002.png", "page-002.txt",
                                                   "page-003.png", "page-003.txt"])
        manifest = json.loads(Path(out, "deck.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest, {k: v for k, v in doc.items() if k not in ("ok", "output_dir")})
        self.assertEqual((manifest["source"]["format"], manifest["source"]["converted_by"]), ("pdf", None))

    def test_pages_render_at_the_fixed_default_dpi(self):
        m = import_deck(self.pdf, os.path.join(self.tmp, "a"))
        self.assertEqual(m["renderer"]["dpi"], 144.0)
        for p, (rgb, _) in zip(m["pages"], DECK_PAGES):
            w, h, colour, first = png_info(os.path.join(self.tmp, "a", p["image"]))
            self.assertEqual((w, h, colour), (1920, 1080, 2))  # 960 x 540 pt at 144 dpi, 8-bit RGB
            self.assertEqual((p["width_px"], p["height_px"], p["width_pt"], p["height_pt"]), (1920, 1080, 960.0, 540.0))
            for got, want in zip(first, rgb):
                self.assertLessEqual(abs(got - want), 1)
        m72 = import_deck(self.pdf, os.path.join(self.tmp, "b"), dpi=72)
        self.assertEqual((m72["pages"][0]["width_px"], m72["pages"][0]["height_px"]), (960, 540))

    def test_output_is_deterministic(self):
        a, b = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        ma, mb = import_deck(self.pdf, a), import_deck(self.pdf, b)
        self.assertEqual(ma, mb)
        for name in sorted(os.listdir(a)):
            self.assertEqual(Path(a, name).read_bytes(), Path(b, name).read_bytes(), name)
        for p in ma["pages"]:
            import hashlib
            self.assertEqual(hashlib.sha256(Path(a, p["image"]).read_bytes()).hexdigest(), p["image_sha256"])

    def test_existing_output_needs_overwrite_and_stale_pages_go(self):
        out = os.path.join(self.tmp, "pages")
        import_deck(self.pdf, out)
        Path(out, "notes.md").write_text("mine", encoding="utf-8")
        short = os.path.join(self.tmp, "short.pdf")
        Path(short).write_bytes(build_pdf(DECK_PAGES[:2]))
        with self.assertRaises(SlideSyncError) as e:
            import_deck(short, out)
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        self.assertTrue(Path(out, "page-003.png").exists())
        m = import_deck(short, out, overwrite=True)
        self.assertEqual(m["page_count"], 2)
        self.assertEqual(sorted(os.listdir(out)), ["deck.json", "notes.md", "page-001.png", "page-001.txt", "page-002.png",
                                                   "page-002.txt"])

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_a_symlink_at_a_page_name_is_replaced_not_followed(self):
        out = os.path.join(self.tmp, "pages")
        os.makedirs(out)
        victim = os.path.join(self.tmp, "victim.txt")
        Path(victim).write_text("keep", encoding="utf-8")
        os.symlink(victim, os.path.join(out, "page-001.txt"))
        os.symlink(os.path.join(self.tmp, "elsewhere.png"), os.path.join(out, "page-001.png"))  # dangling
        with self.assertRaises(SlideSyncError):
            import_deck(self.pdf, out)
        import_deck(self.pdf, out, overwrite=True)
        self.assertEqual(Path(victim).read_text(encoding="utf-8"), "keep")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "elsewhere.png")))
        self.assertFalse(os.path.islink(os.path.join(out, "page-001.txt")))
        self.assertEqual(Path(self.pdf).read_bytes(), build_pdf(DECK_PAGES))  # the deck is never written

    def test_a_file_that_is_not_a_pdf(self):
        bad = os.path.join(self.tmp, "bad.pdf")
        Path(bad).write_bytes(b"not a pdf at all")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["import", bad, "-o", os.path.join(self.tmp, "o"), "--json"])
        self.assertEqual((rc, json.loads(buf.getvalue())["error"]["code"]), (2, "INVALID_DECK"))

    def test_pptx_goes_through_libreoffice_then_pdfium(self):
        pptx = os.path.join(self.tmp, "talk.pptx")
        build_pptx(pptx, [["x"]])

        def runner(argv, timeout):  # stands in for soffice: writes the converted deck where LibreOffice would
            Path(argv[argv.index("--outdir") + 1], "deck.pdf").write_bytes(build_pdf(DECK_PAGES))
            return 0, "", ""
        m = import_deck(pptx, os.path.join(self.tmp, "o"), soffice=sys.executable, runner=runner)
        self.assertEqual((m["page_count"], [p["text"] for p in m["pages"]]), (3, DECK_TEXT))
        self.assertEqual((m["source"]["format"], m["source"]["converted_by"]["tool"]), ("pptx", "libreoffice"))


# ------------------------------------------------------------------ match: pages and frames drawn here

def draw(width, height, bg, rects):
    """Raw RGB rows: a background and filled rectangles (x, y, w, h, rgb), the stand-in for slide text and shapes."""
    rows = [bytearray(bytes(bg) * width) for _ in range(height)]
    for x, y, w, h, rgb in rects:
        for yy in range(max(0, y), min(height, y + h)):
            rows[yy][3 * max(0, x):3 * min(width, x + w)] = bytes(rgb) * (min(width, x + w) - max(0, x))
    return [bytes(r) for r in rows]


def page_rects(i, width=320, height=180):
    """A slide of one template: the same title bar on every page, then 'lines of text' whose count, lengths and
    indents differ from page to page."""
    rects = [(0, 0, width, 22, (40, 60, 120)), (16, 30, 60 + 23 * (i % 5), 10, (20, 20, 20))]
    for k in range(2 + i % 4):
        length = 40 + (i * 37 + k * 53) % 200
        rects.append((24 + 12 * ((i + k) % 3), 58 + 24 * k, length, 7, (30, 30, 30)))
    if i % 3 == 0:
        rects.append((220, 120, 70, 45, (200, 80, 40)))  # a figure
    return rects


def write_png(path, width, height, rows):
    Path(path).write_bytes(deck_mod.png_bytes(width, height, b"".join(rows), width * 3, 3))


def write_deck(directory, pages, size=(320, 180)):
    """A deck directory as `import` writes it (deck.json + page PNGs), from `pages`: list of rect lists."""
    os.makedirs(directory, exist_ok=True)
    out = []
    for n, rects in enumerate(pages, 1):
        name = f"page-{n:03d}.png"
        write_png(os.path.join(directory, name), size[0], size[1], draw(size[0], size[1], (255, 255, 255), rects))
        out.append({"index": n, "image": name, "text_file": f"page-{n:03d}.txt", "width_px": size[0], "height_px": size[1]})
    Path(directory, "deck.json").write_text(json.dumps({"schema": DECK_SCHEMA, "source": {"path": "deck.pdf"},
                                                        "page_count": len(out), "pages": out}), encoding="utf-8")


def frame_rows(page_rows, pw, ph, fw, fh, box=None, surround=(16, 16, 16)):
    """A video frame: the page scaled (nearest) into `box` (x, y, w, h) of an fw x fh frame of `surround`, or filling it."""
    x0, y0, bw, bh = box or (0, 0, fw, fh)
    out = []
    for y in range(fh):
        if not y0 <= y < y0 + bh:
            out.append(bytes(surround) * fw)
            continue
        src = page_rows[(y - y0) * ph // bh]
        row = bytearray(bytes(surround) * fw)
        for x in range(x0, x0 + bw):
            sx = (x - x0) * pw // bw
            row[3 * x:3 * x + 3] = src[3 * sx:3 * sx + 3]
        out.append(bytes(row))
    return out


class LookRunner:
    """Stands in for ffmpeg-skill look: writes, for every --at, the frame the script says is on screen then."""

    def __init__(self, script, frames):
        self.script = script  # [(start, end, key)]
        self.frames = frames  # key -> (width, height, rows)
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        assert Path(argv[1]).name == "look.py"
        base = argv[argv.index("-o") + 1]
        outputs = []
        for i, a in enumerate(argv):
            if a == "--at":
                t = float(argv[i + 1])
                key = next(k for s, e, k in self.script if s <= t < e)
                w, h, rows = self.frames[key]
                out = f"{base}_{t:.3f}s.png"
                write_png(out, w, h, rows)
                outputs.append(out)
        return 0, json.dumps({"status": "completed", "output": None, "outputs": outputs}), ""


def match_timeline(video, script, duration):
    return {"schema": TIMELINE_SCHEMA, "source": {"path": os.path.abspath(video), "duration": duration},
            "slides": [{"index": n, "start": s, "end": e, "duration": e - s} for n, (s, e, _) in enumerate(script, 1)]}


class MatchUnitTests(unittest.TestCase):
    """The matcher on pictures drawn here, with ffmpeg-skill look faked: no ffmpeg, no PDF engine."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.video = os.path.join(cls.tmp, "talk.mp4")
        Path(cls.video).write_bytes(b"x")
        cls.pages = [page_rects(i) for i in range(1, 9)]
        cls.deck = os.path.join(cls.tmp, "deck")
        write_deck(cls.deck, cls.pages)
        cls.page_rows = {n: draw(320, 180, (255, 255, 255), r) for n, r in enumerate(cls.pages, 1)}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def frame(self, key, **kw):
        if key == "black":
            return 640, 360, [bytes(640 * 3)] * 360
        rows = self.page_rows.get(key) or draw(320, 180, (255, 255, 255), page_rects(key))  # key 11: not in the deck
        return 640, 360, frame_rows(rows, 320, 180, 640, 360, **kw)

    def test_shuffled_screen_and_letterboxed_segments(self):
        # back and forth, skips, a page shown letterboxed, a slide that is not in the deck, the projector blanked
        shown = [(1, {}), (2, {}), (5, {}), (3, {"box": (80, 45, 480, 270)}), (2, {}), (11, {}), (8, {}), ("black", {}),
                 (5, {"box": (40, 22, 560, 315)}), (6, {})]
        script = [(4.0 * n, 4.0 * (n + 1), n) for n in range(len(shown))]
        frames = {n: self.frame(key, **kw) for n, (key, kw) in enumerate(shown)}
        runner = LookRunner(script, frames)
        m = match(SKILL, self.video, self.deck, match_timeline(self.video, script, 40.0), samples=1, runner=runner)
        self.assertEqual(m["schema"], MATCH_SCHEMA)
        self.assertEqual([s["page"] for s in m["segments"]], [1, 2, 5, 3, 2, None, 8, None, 5, 6])
        self.assertEqual([s["reason"] for s in m["segments"]][5:8], ["below_min_score", None, "below_min_score"])
        self.assertEqual(m["segments"][3]["view"], "located")
        self.assertEqual(m["segments"][0]["view"], "full")
        for s in m["segments"]:
            self.assertEqual(s["method"], "luma-grid-zncc")
            if s["page"] is not None:
                self.assertGreaterEqual(s["confidence"], match_mod.DEFAULT_MIN_SCORE)
                self.assertGreaterEqual(s["margin"], match_mod.DEFAULT_MIN_MARGIN)
        self.assertEqual((m["matched_count"], m["pages_shown"], m["pages_not_shown"]), (8, [1, 2, 3, 5, 6, 8], [4, 7]))
        argv = runner.calls[0]
        self.assertEqual(len(runner.calls), 1)  # one look call for every frame
        self.assertEqual(argv[argv.index("--width") + 1], str(match_mod.FRAME_WIDTH))
        self.assertIn("--no-timecode", argv)
        self.assertEqual(argv[-1], "--json")

    def test_identical_pages_are_ambiguous_not_guessed(self):
        deck = os.path.join(self.tmp, "dup")
        write_deck(deck, [self.pages[0], self.pages[1], self.pages[2], self.pages[1]])  # page 4 repeats page 2
        script = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        runner = LookRunner(script, {0: self.frame(1), 1: self.frame(2)})
        m = match(SKILL, self.video, deck, match_timeline(self.video, script, 10.0), samples=1, runner=runner)
        first, second = m["segments"]
        self.assertEqual(first["page"], 1)
        self.assertEqual((second["page"], second["reason"], second["margin"]), (None, "ambiguous", 0.0))
        self.assertEqual({c["page"] for c in second["candidates"][:2]}, {2, 4})

    def test_frames_that_disagree_are_reported(self):
        # detect missed the change at 6 s: one segment holds page 4 then page 7
        script = [(0.0, 6.0, "a"), (6.0, 12.0, "b")]
        runner = LookRunner(script, {"a": self.frame(4), "b": self.frame(7)})
        tl = match_timeline(self.video, [(0.0, 12.0, None)], 12.0)
        m = match(SKILL, self.video, self.deck, tl, samples=3, runner=runner)
        seg = m["segments"][0]
        self.assertEqual([s["page"] for s in seg["samples"]], [4, 7, 7])
        self.assertTrue(seg["mixed"])
        self.assertEqual(seg["page"], 7)  # the median: two of three frames
        self.assertTrue(any("detect may have missed a change" in w for w in m["warnings"]))

    def test_thresholds_decide(self):
        self.assertEqual(decide({1: 0.9, 2: 0.5}, min_score=0.7, min_margin=0.1)["page"], 1)
        self.assertEqual(decide({1: 0.65, 2: 0.1}, min_score=0.7, min_margin=0.1)["reason"], "below_min_score")
        self.assertEqual(decide({1: 0.9, 2: 0.85}, min_score=0.7, min_margin=0.1)["reason"], "ambiguous")
        d = decide({3: 0.8}, min_score=0.7, min_margin=0.1)  # a one-page deck: no runner-up
        self.assertEqual((d["page"], d["candidates"]), (3, [{"page": 3, "score": 0.8}]))

    def test_sample_times_stay_inside_the_segment_and_the_video(self):
        self.assertEqual(sample_times(10.0, 14.0, 3), [11.05, 12.0, 12.95])
        self.assertEqual(sample_times(10.0, 10.1, 2), [10.05, 10.05])
        self.assertTrue(all(t <= 19.9 for t in sample_times(18.0, 25.0, 3, duration=20.0)))

    def test_keystoned_slide_is_found_as_a_quadrilateral(self):
        gw, gh = 256, 144
        quad = ((40.3, 20.6), (214.2, 12.4), (222.7, 128.1), (31.5, 133.2))

        def inside(x, y):  # convex quad, corners clockwise in image coordinates
            sign = []
            for (ax, ay), (bx, by) in zip(quad, quad[1:] + quad[:1]):
                sign.append((bx - ax) * (y - ay) - (by - ay) * (x - ax) >= 0)
            return all(sign)
        luma = []
        for j in range(gh):
            for i in range(gw):  # 4 x 4 supersampling: a cell on the edge gets its covered fraction
                cover = sum(inside(i + (a + 0.5) / 4, j + (b + 0.5) / 4) for a in range(4) for b in range(4)) / 16
                luma.append(30 + 200 * cover)
        found = slide_quad(luma, gw, gh)
        self.assertIsNotNone(found)
        for (fx, fy), (qx, qy) in zip(found, quad):
            self.assertLess(abs(fx - qx), 0.5)
            self.assertLess(abs(fy - qy), 0.5)
        to_xy = quad_sampler(quad)
        for (u, v), (qx, qy) in zip(((0, 0), (1, 0), (1, 1), (0, 1)), quad):
            x, y = to_xy(u, v)
            self.assertAlmostEqual(x, qx, places=6)
            self.assertAlmostEqual(y, qy, places=6)
        self.assertIsNone(slide_quad([100.0 + (i * 7919 % 97) for i in range(gw * gh)], gw, gh))  # no surround

    def test_png_reader_undoes_every_filter_and_format(self):
        width, height = 7, 5
        pixels = [bytes((x * 37 + y * 11 + c * 50) % 256 for x in range(width) for c in range(3)) for y in range(height)]

        def paeth(a, b, c):
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            return a if pa <= pb and pa <= pc else (b if pb <= pc else c)

        def encode(rows, bpp, colour, depth=8, plte=b""):
            raw, prev = b"", bytes(len(rows[0]))
            for y, row in enumerate(rows):
                f = y % 5
                line = bytearray()
                for i, v in enumerate(row):
                    a = row[i - bpp] if i >= bpp else 0
                    b, c = prev[i], prev[i - bpp] if i >= bpp else 0
                    pred = (0, a, b, (a + b) // 2, paeth(a, b, c))[f]
                    line.append((v - pred) & 0xFF)
                raw += bytes([f]) + bytes(line)
                prev = row
            w = len(rows[0]) // bpp

            def chunk(kind, data):
                return len(data).to_bytes(4, "big") + kind + data + (zlib.crc32(kind + data) & 0xFFFFFFFF).to_bytes(4, "big")
            ihdr = w.to_bytes(4, "big") + len(rows).to_bytes(4, "big") + bytes([depth, colour, 0, 0, 0])
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + (chunk(b"PLTE", plte) if plte else b"")
                    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.png")
            Path(p).write_bytes(encode(pixels, 3, 2))
            self.assertEqual(read_png(p), (width, height, 3, pixels))
            rgba = [bytes(v for x in range(width) for v in row[3 * x:3 * x + 3] + b"\x80") for row in pixels]
            Path(p).write_bytes(encode(rgba, 4, 6))
            self.assertEqual(read_png(p), (width, height, 4, rgba))
            grey16 = [bytes(v for x in range(width) for v in (row[3 * x], 0x55)) for row in pixels]
            Path(p).write_bytes(encode(grey16, 2, 0, depth=16))
            self.assertEqual(read_png(p), (width, height, 1, [row[0::3] for row in pixels]))
            palette = bytes(range(0, 255, 85)) * 3  # 4 colours
            Path(p).write_bytes(encode([bytes([x % 4 for x in range(width)])] * 2, 1, 3, plte=palette[:12]))
            self.assertEqual(read_png(p)[3][0][:6], palette[0:3] + palette[3:6])
            Path(p).write_bytes(b"not a png")
            with self.assertRaises(SlideSyncError) as e:
                read_png(p)
            self.assertEqual(e.exception.code, "INVALID_IMAGE")

    def test_cli_errors(self):
        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch.object(cli, "_engine", return_value=SKILL):
                rc = cli.main(list(argv) + ["--json"])
            return rc, json.loads(buf.getvalue())
        tl = os.path.join(self.tmp, "tl.json")
        Path(tl).write_text(json.dumps(match_timeline(self.video, [(0.0, 4.0, 0)], 4.0)), encoding="utf-8")
        rc, doc = run("match", self.video, "--deck", os.path.join(self.tmp, "nowhere"), "--timeline", tl)
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_DECK"))
        rc, doc = run("match", self.video, "--deck", self.deck, "--timeline", tl, "--samples", "0")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        rc, doc = run("match", self.video, "--deck", self.deck, "--timeline", tl, "-o", tl)
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))  # refuses to overwrite without --overwrite
        Path(self.tmp, "bad.json").write_text(json.dumps({"schema": "x"}), encoding="utf-8")
        rc, doc = run("match", self.video, "--deck", self.deck, "--timeline", os.path.join(self.tmp, "bad.json"))
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_TIMELINE"))


SOFFICE = find_soffice()


@unittest.skipUnless(HAS_PDFIUM and SOFFICE, "needs LibreOffice and pypdfium2")
class LibreOfficeIntegrationTests(unittest.TestCase):
    """The real soffice on a .pptx built here."""

    def test_pptx_page_count_and_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            pptx = os.path.join(tmp, "talk.pptx")
            build_pptx(pptx, [["Introduction"], ["研究の背景", "日本語のスライド"], ["Thanks / ありがとうございました"]])
            m = import_deck(pptx, os.path.join(tmp, "pages"), timeout=300)
            self.assertEqual(m["page_count"], 3)
            self.assertEqual([p["text"] for p in m["pages"]],
                             ["Introduction", "研究の背景\n日本語のスライド", "Thanks / ありがとうございました"])
            self.assertEqual(sorted(os.listdir(tmp)), ["pages", "talk.pptx"])  # no lock file next to the deck
            # LibreOffice writes a 16:9 slide as 960.0095 x 540 pt, so it is ceil(1920.02) = 1921 px wide at 144 dpi
            for p in m["pages"]:
                self.assertEqual((p["width_px"], p["height_px"]), (math.ceil(p["width_pt"] * 2 - 1e-9), math.ceil(p["height_pt"] * 2 - 1e-9)))
                self.assertLessEqual(abs(p["width_px"] - 1920), 1)


ENGINE = locate()


@unittest.skipUnless(ENGINE and shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg-skill and ffmpeg")
class IntegrationTests(unittest.TestCase):
    """Real ffmpeg-skill on generated decks: solid-colour slides of known length (the fixture uses ffmpeg directly;
    the skill never does)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def deck(self, name, slides):
        parts = []
        for i, (colour, seconds) in enumerate(slides):
            p = os.path.join(self.tmp, f"{name}_{i}.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=640x360:d={seconds}:r=25",
                            "-pix_fmt", "yuv420p", p], check=True)
            parts.append(p)
        lst = os.path.join(self.tmp, f"{name}.txt")
        Path(lst).write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
        out = os.path.join(self.tmp, f"{name}.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", out], check=True)
        return out

    def test_changes_within_one_frame(self):
        video = self.deck("four", [("white", 4), ("red", 5), ("blue", 3), ("green", 4)])
        tl = detect(ENGINE, video, min_dwell=2)
        self.assertEqual(tl["slide_count"], 4)
        for got, want in zip(tl["changes"], [4.0, 9.0, 12.0]):
            self.assertAlmostEqual(got, want, delta=0.04 + 1e-6)  # one frame at 25 fps

    def test_a_flash_shorter_than_min_dwell_is_not_a_slide(self):
        video = self.deck("flash", [("white", 4), ("red", 0.4), ("blue", 4)])
        tl = detect(ENGINE, video, min_dwell=2)
        self.assertEqual(tl["slide_count"], 2)

    def test_chapters_land_in_the_container(self):
        video = self.deck("ch", [("white", 12), ("red", 12), ("blue", 12)])
        out = os.path.join(self.tmp, "ch_out.mp4")
        buf = io.StringIO()
        titles = os.path.join(self.tmp, "titles.txt")
        Path(titles).write_text("はじめに\n方法\n結果\n", encoding="utf-8")
        with redirect_stdout(buf):
            rc = cli.main(["chapters", video, "--titles", titles, "-o", out, "--json", "--ffmpeg-skill", ENGINE.root])
        doc = json.loads(buf.getvalue())
        self.assertEqual((rc, doc["chapter_count"], doc["warnings"]), (0, 3, []))
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", out], capture_output=True, text=True, check=True)
        got = [(round(float(c["start_time"])), c["tags"]["title"]) for c in json.loads(probe.stdout)["chapters"]]
        self.assertEqual(got, [(0, "はじめに"), (12, "方法"), (24, "結果")])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["chapters", video, "-o", out, "--json", "--ffmpeg-skill", ENGINE.root]), 4)

    @staticmethod
    def colour_at(video, t):
        """Mean RGB of the frame at `t` seconds (fixture check through ffmpeg directly)."""
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", "-vf", "scale=1:1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        return tuple(raw[:3])

    def test_inserted_clips_shift_the_slides_and_chapters(self):
        """A demo video (another size, frame rate, with sound) after slide 1 and a still after slide 2, through the real
        ffmpeg-skill probe / insert / cut / join / metadata: chapters in the output, later slides shifted."""
        video = self.deck("ins", [("white", 4), ("red", 4), ("blue", 4)])
        demo = os.path.join(self.tmp, "demo.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=green:s=320x240:d=2:r=30", "-f", "lavfi",
                        "-i", "sine=f=440:d=2", "-shortest", "-pix_fmt", "yuv420p", "-c:a", "aac", demo], check=True)
        card = os.path.join(self.tmp, "card.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=yellow:s=640x360", "-frames:v", "1", card],
                       check=True)
        tl_file, out, shifted = (os.path.join(self.tmp, n) for n in ("ins_tl.json", "ins_out.mp4", "ins_shifted.json"))
        Path(tl_file).write_text(json.dumps(detect(ENGINE, video, min_dwell=2)), encoding="utf-8")
        titles = os.path.join(self.tmp, "ins_titles.txt")
        Path(titles).write_text("はじめに\n方法\n結果\n", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["insert", video, "--timeline", tl_file, "--insert", f"1={demo}", "--insert", f"2={card}",
                           "--insert-title", "デモ", "--insert-title", "Card", "--still-duration", "1.5", "--titles", titles,
                           "-o", out, "--timeline-out", shifted, "--json", "--ffmpeg-skill", ENGINE.root])
        doc = json.loads(buf.getvalue())
        self.assertEqual(rc, 0, doc)
        want = [(0.0, "はじめに"), (4.0, "デモ"), (6.0, "方法"), (10.0, "Card"), (11.5, "結果")]
        self.assertEqual([c["title"] for c in doc["chapters"]], [w[1] for w in want])
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-of", "json", out],
                               capture_output=True, text=True, check=True)
        info = json.loads(probe.stdout)
        got = [(float(c["start_time"]), c["tags"]["title"]) for c in info["chapters"]]
        for (gt, gtitle), (wt, wtitle) in zip(got, want):
            self.assertEqual(gtitle, wtitle)
            self.assertAlmostEqual(gt, wt, delta=0.04 + 1e-6)  # one frame at 25 fps
        self.assertEqual(len(got), len(want))
        self.assertAlmostEqual(float(info["format"]["duration"]), 15.5, delta=0.1)
        tl = json.loads(Path(shifted).read_text(encoding="utf-8"))
        self.assertEqual((tl["provenance"], tl["derived_from"]["provenance"]), ("DERIVED", "OBSERVED"))
        self.assertEqual([(e["after_slide"], e["clip_kind"]) for e in tl["edits"]], [(1, "video"), (2, "still")])
        self.assertLessEqual(abs(tl["render"]["drift"]), 0.04 + 1e-6)
        # the picture is where the shifted timeline says: the middle of each segment shows its colour
        colours = {"slide": {1: (255, 255, 255), 2: (255, 0, 0), 3: (0, 0, 255)}, "insert": {1: (0, 128, 0), 2: (255, 255, 0)}}
        for seg in tl["slides"]:
            want_rgb = colours[seg["kind"]][seg.get("source_slide") or seg["insert"]]
            got_rgb = self.colour_at(out, (seg["start"] + seg["end"]) / 2)
            self.assertTrue(all(abs(g - w) <= 40 for g, w in zip(got_rgb, want_rgb)), (seg, got_rgb, want_rgb))


def _latin(*lines):
    return [("latin", t) for t in lines]


# The shuffled-slides deck: one template (black text on a coloured page), distinct titles and bullets.
SHUFFLE_DECK = [
    ((255, 255, 255), _latin("Slide Sync", "Matching talk recordings to decks", "A. Author")),
    ((120, 170, 255), _latin("Background", "- conference talks are recorded", "- slides are shared as PDF", "- chapters need pages")),
    ((255, 190, 90), _latin("Method", "- frames from ffmpeg-skill", "- luma grid per picture", "- correlation with every page",
                            "- median over frames")),
    ((60, 190, 90), _latin("Results", "- every segment matched", "- unknown slides left null")),
    ((240, 120, 160), _latin("Discussion", "- keystone from a camera", "- people in front", "- animated builds",
                             "- videos in slides", "- zoomed shots")),
    ((170, 170, 170), _latin("Thank you", "questions welcome")),
]
SHUFFLE_EXTRA = ((255, 255, 255), _latin("Appendix", "- not in the shared deck", "- extra backup slide"))
# What the presenter shows, (page, seconds): back and forth, skipping, a backup slide that is not in the deck ("x") and
# the projector blanked ("b"). The pages expected for each detected segment, None where no page may be claimed.
SHUFFLE_SHOW = [(1, 4), (2, 4), (4, 4), (3, 4), (2, 3), ("x", 4), (5, 4), ("b", 3), (3, 3), (6, 4)]
SHUFFLE_PAGES = [p if isinstance(p, int) else None for p, _ in SHUFFLE_SHOW]


@unittest.skipUnless(ENGINE and shutil.which("ffmpeg") and HAS_PDFIUM, "needs ffmpeg-skill, ffmpeg and pypdfium2")
class ShuffledSlidesTests(unittest.TestCase):
    """Roadmap step 4's done-when fixture: a deck imported for real, shown out of order in a recording made here with
    ffmpeg (the fixture runs ffmpeg directly; the skill never does), detected and matched by the real ffmpeg-skill."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        t = cls.tmp
        Path(t, "deck.pdf").write_bytes(build_pdf(SHUFFLE_DECK))
        Path(t, "extra.pdf").write_bytes(build_pdf([SHUFFLE_EXTRA]))
        import_deck(os.path.join(t, "deck.pdf"), os.path.join(t, "pages"), dpi=72)
        import_deck(os.path.join(t, "extra.pdf"), os.path.join(t, "extra"), dpi=72)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=960x540", "-frames:v", "1",
                        os.path.join(t, "black.png")], check=True)

        def image(p):
            if isinstance(p, int):
                return os.path.join(t, "pages", f"page-{p:03d}.png")
            return os.path.join(t, "extra", "page-001.png") if p == "x" else os.path.join(t, "black.png")
        lst = os.path.join(t, "show.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for p, seconds in SHUFFLE_SHOW:
                fh.write(f"file '{image(p)}'\nduration {seconds}\n")
            fh.write(f"file '{image(SHUFFLE_SHOW[-1][0])}'\n")
        # a screen capture, and a camera off the projector's axis: slide in a dark room, keystoned, with sensor noise
        looks = {"screen": "scale=1280:720",
                 "camera": "scale=1000:562,pad=1280:720:140:79:0x202020,perspective=x0=60:y0=40:x1=1230:y1=0:x2=0:y2=720"
                           ":x3=1280:y3=670:sense=destination,noise=alls=12:allf=t"}
        cls.videos = {}
        for name, vf in looks.items():
            out = os.path.join(t, f"talk_{name}.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-vf",
                            vf + ",fps=25,format=yuv420p", "-c:v", "libx264", "-crf", "26", out], check=True)
            cls.videos[name] = out
        cls.timeline = detect(ENGINE, cls.videos["screen"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_detect_finds_every_change(self):
        starts = [0.0]
        for _, seconds in SHUFFLE_SHOW[:-1]:
            starts.append(starts[-1] + seconds)
        self.assertEqual(self.timeline["slide_count"], len(SHUFFLE_SHOW))
        for got, want in zip(self.timeline["changes"], starts[1:]):
            self.assertAlmostEqual(got, want, delta=0.04 + 1e-6)

    def test_screen_capture_maps_every_segment_to_its_page(self):
        out = os.path.join(self.tmp, "match.json")
        tl = os.path.join(self.tmp, "timeline.json")
        Path(tl).write_text(json.dumps(self.timeline), encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["match", self.videos["screen"], "--deck", os.path.join(self.tmp, "pages"), "--timeline", tl,
                           "-o", out, "--json", "--ffmpeg-skill", ENGINE.root])
        doc = json.loads(buf.getvalue())
        self.assertEqual((rc, doc["ok"], doc["schema"]), (0, True, MATCH_SCHEMA))
        self.assertEqual([s["page"] for s in doc["segments"]], SHUFFLE_PAGES)
        self.assertEqual(json.loads(Path(out).read_text(encoding="utf-8")),
                         {k: v for k, v in doc.items() if k not in ("ok", "match_file")})
        for s in doc["segments"]:
            self.assertEqual(s["reason"] is None, s["page"] is not None)
            self.assertFalse(s["mixed"])
        self.assertEqual(doc["pages_not_shown"], [])
        self.assertEqual(doc["warnings"], ["2 segment(s) not matched to a page: 6, 8"])

    def test_keystoned_camera_shot_maps_every_segment_to_its_page(self):
        m = match(ENGINE, self.videos["camera"], os.path.join(self.tmp, "pages"), self.timeline)
        self.assertEqual([s["page"] for s in m["segments"]], SHUFFLE_PAGES)
        self.assertTrue(all(s["view"] == "located" for s in m["segments"] if s["page"] is not None))
        self.assertTrue(any("measured on" in w for w in m["warnings"]))  # the timeline came from the screen recording


if __name__ == "__main__":
    unittest.main(verbosity=2)
