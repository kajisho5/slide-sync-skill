"""slide-sync-skill tests.

Unit tests fake ffmpeg-skill's answers. Integration tests run the real ffmpeg-skill on generated slide decks;
they are skipped unless ffmpeg-skill is found (FFMPEG_SKILL_DIR, ../ffmpeg-skill, ...) and ffmpeg is on PATH.
Import tests build their PDF / PPTX decks here at test time; they need pypdfium2 (`pip install -e '.[pdf]'`), and the
real-LibreOffice test also needs soffice.

    python -m unittest -v tests/test_slide_sync.py
"""
from __future__ import annotations

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
        self.assertIn("slides.import", {p["id"] for p in c["provides"]})
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
