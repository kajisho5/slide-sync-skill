"""slide-sync-skill tests.

Unit tests fake ffmpeg-skill's answers. Integration tests run the real ffmpeg-skill on generated slide decks;
they are skipped unless ffmpeg-skill is found (FFMPEG_SKILL_DIR, ../ffmpeg-skill, ...) and ffmpeg is on PATH.

    python -m unittest -v tests/test_slide_sync.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slide_sync import __version__  # noqa: E402
from slide_sync import cli  # noqa: E402
from slide_sync.contract import ERRORS, contract  # noqa: E402
from slide_sync.core import TIMELINE_SCHEMA, SlideSyncError, chapters, detect, timecode, write_chapters  # noqa: E402
from slide_sync.engine import EngineError, FfmpegSkill, locate, run_tool  # noqa: E402

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


SKILL = FfmpegSkill("/fake/ffmpeg-skill", "2.21.0", ["metadata", "probe", "scenes"])


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


class ContractTests(unittest.TestCase):
    def test_os_registry_keys_and_provides(self):
        c = contract()
        for k in ("skill_id", "id", "skill", "version", "contract_version", "provides", "dependencies", "not_provided"):
            self.assertIn(k, c)
        self.assertEqual(c["version"], __version__)
        self.assertEqual({p["tool_id"] for p in c["provides"]}, {t["tool_id"] for t in c["tools"]})
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
