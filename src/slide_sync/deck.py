"""Slide import: a deck file becomes one PNG and one text per page, plus a manifest (`slide-sync/deck@1`).

A PDF is read with pypdfium2 (PDFium; Apache-2.0 OR BSD-3-Clause), an optional extra: `pip install
'slide-sync-skill[pdf]'`. A .pptx / .ppt / .key / .odp deck is first converted to PDF by LibreOffice
(`soffice --headless --convert-to pdf`) when it is installed; nothing else converts it. See docs/decisions.md ADR-001.

Output is deterministic for a given input, DPI and pypdfium2 build: pages are rendered at a fixed DPI (default 144,
so a 960 x 540 pt 16:9 slide becomes 1920 x 1080 px; in general ceil(points x dpi / 72)) onto white, written as
8-bit RGB PNG by this module (zlib level 9, no timestamps or other metadata), and the text is normalised the same way
every time (`normalise_text`).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unicodedata
import zlib
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from .core import SlideSyncError

DECK_SCHEMA = "slide-sync/deck@1"
DEFAULT_DPI = 144.0
MIN_DPI, MAX_DPI = 18.0, 600.0
MAX_PAGE_PIXELS = 64_000_000  # one page bitmap; 64 MP is a 16:9 page at ~600 dpi on a 13.3 in slide
PDF_EXT = ".pdf"
OFFICE_EXTS = (".pptx", ".ppt", ".key", ".odp")
MANIFEST = "deck.json"
PAGE_FILE = re.compile(r"^page-\d{3,}\.(png|txt)$")
ENV_SOFFICE = "SLIDE_SYNC_SOFFICE"
PDF_EXTRA_INSTALL = "pip install 'slide-sync-skill[pdf]'  (pypdfium2, Apache-2.0 OR BSD-3-Clause)"
SOFFICE_INSTALL = ("install LibreOffice (https://www.libreoffice.org; apt install libreoffice-impress, brew install --cask "
                   "libreoffice, winget install TheDocumentFoundation.LibreOffice) or export the deck as PDF")
TEXT_NORMALISATION = ("CRLF/CR -> LF; control characters other than LF and TAB dropped; Unicode NFC; CJK radicals "
                      "(U+2E80-U+2FDF) -> NFKC (e.g. U+2F47 -> U+65E5); trailing spaces of each line and blank lines at "
                      "both ends removed")

Runner = Callable[[List[str], float], Tuple[int, str, str]]


# ---------------------------------------------------------------- dependencies

def pdfium_status() -> Dict[str, Any]:
    """pypdfium2 present? Looked up without importing it, so doctor stays cheap."""
    try:
        found = importlib.util.find_spec("pypdfium2") is not None
    except (ImportError, ValueError):
        found = False
    version = None
    if found:
        try:
            from importlib.metadata import PackageNotFoundError, version as _v
            try:
                version = _v("pypdfium2")
            except PackageNotFoundError:
                version = None
        except ImportError:  # pragma: no cover
            version = None
    return {"found": found, "version": version, "licence": "Apache-2.0 OR BSD-3-Clause (PDFium: BSD-3-Clause)",
            "install": None if found else PDF_EXTRA_INSTALL}


def _pdfium() -> Any:
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        raise SlideSyncError("DEPENDENCY_MISSING", "pypdfium2 is not installed: " + PDF_EXTRA_INSTALL)
    return pypdfium2


def _pdfium_versions(pdfium: Any) -> Dict[str, Optional[str]]:
    v = getattr(pdfium, "version", None)
    py = getattr(v, "PYPDFIUM_INFO", None) or getattr(pdfium, "V_PYPDFIUM2", None)
    lib = getattr(v, "PDFIUM_INFO", None) or getattr(pdfium, "V_LIBPDFIUM", None)
    return {"pypdfium2": str(py) if py is not None else None, "pdfium": str(lib) if lib is not None else None}


def soffice_candidates(explicit: Optional[str] = None) -> List[str]:
    out = [explicit] if explicit else []
    if os.environ.get(ENV_SOFFICE):
        out.append(os.environ[ENV_SOFFICE])
    out += [p for p in (shutil.which("soffice"), shutil.which("libreoffice")) if p]
    if sys.platform == "darwin":
        out.append("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    elif os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                out.append(os.path.join(base, "LibreOffice", "program", "soffice.exe"))
    return out


def find_soffice(explicit: Optional[str] = None) -> Optional[str]:
    for c in soffice_candidates(explicit):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.realpath(c)
    return None


def soffice_status(explicit: Optional[str] = None) -> Dict[str, Any]:
    path = find_soffice(explicit)
    return {"found": path is not None, "path": path, "licence": "MPL-2.0", "used_for": "pptx / ppt / key / odp -> pdf",
            "install": None if path else SOFFICE_INSTALL}


def subprocess_runner(argv: List[str], timeout: float) -> Tuple[int, str, str]:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False,
                       stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def convert_to_pdf(deck: str, workdir: str, *, soffice: Optional[str] = None, timeout: float = 600.0,
                   runner: Runner = subprocess_runner) -> Tuple[str, str]:
    """LibreOffice converts `deck` to a PDF inside `workdir`. The deck is copied into `workdir` first, so nothing
    (not even LibreOffice's lock file) is written next to the input. Returns (pdf path, soffice path)."""
    binary = find_soffice(soffice)
    if binary is None:
        raise SlideSyncError("DEPENDENCY_MISSING", f"LibreOffice (soffice) is needed to import {os.path.splitext(deck)[1]} "
                             "decks and was not found: " + SOFFICE_INSTALL)
    ext = os.path.splitext(deck)[1].lower()
    src = os.path.join(workdir, "deck" + ext)
    shutil.copyfile(deck, src)
    outdir = os.path.join(workdir, "pdf")
    os.mkdir(outdir)
    profile = os.path.join(workdir, "lo-profile")
    argv = [binary, f"-env:UserInstallation={_file_uri(profile)}", "--headless", "--norestore", "--nologo",
            "--convert-to", "pdf", "--outdir", outdir, src]
    try:
        rc, out, err = runner(argv, timeout)
    except subprocess.TimeoutExpired:
        raise SlideSyncError("CONVERT_TIMEOUT", f"LibreOffice did not finish converting within {timeout:.0f}s")
    pdf = os.path.join(outdir, "deck.pdf")
    if rc != 0 or not os.path.isfile(pdf):
        last = ((err or out).strip().splitlines() or [""])[-1]
        raise SlideSyncError("CONVERT_FAILED", f"LibreOffice could not convert {os.path.basename(deck)} to PDF"
                             + (f" (exit {rc})" if rc else "") + (f": {last}" if last else ""))
    return pdf, binary


def _file_uri(path: str) -> str:
    from pathlib import Path
    return Path(os.path.abspath(path)).as_uri()


# ---------------------------------------------------------------- text and png

_DROP = {c for c in range(0x20) if c not in (0x09, 0x0A)} | {0x7F, 0xFFFE, 0xFFFF}


def normalise_text(text: str) -> str:
    """Page text as PDFium extracted it, made stable and comparable (see TEXT_NORMALISATION). Decks exported from
    PowerPoint / Keynote often map 日 and other characters to their Kangxi radical twin (U+2F47 ⽇); those are folded
    to the unified ideograph. Full-width Latin letters and half-width katakana are left as they are."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ord(ch) not in _DROP)
    text = unicodedata.normalize("NFC", text)
    text = "".join(unicodedata.normalize("NFKC", ch) if 0x2E80 <= ord(ch) <= 0x2FDF else ch for ch in text)
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip("\n")


def png_bytes(width: int, height: int, rows: bytes, stride: int, channels: int) -> bytes:
    """8-bit RGB (3 channels) or RGBA (4) PNG, filter 0 on every row, zlib level 9, no ancillary chunks: the same
    pixels always give the same bytes."""
    colour = {3: 2, 4: 6}[channels]
    row = width * channels
    raw = b"".join(b"\x00" + rows[y * stride:y * stride + row] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- import

def _page_name(i: int, count: int) -> str:
    return f"page-{i:0{max(3, len(str(count)))}d}"


def _render_pdf(pdf: str, dpi: float) -> Tuple[List[Dict[str, Any]], Dict[str, Optional[str]]]:
    pdfium = _pdfium()
    try:
        doc = pdfium.PdfDocument(pdf)
    except Exception as e:  # PdfiumError: not a PDF, damaged, or password-protected
        raise SlideSyncError("INVALID_DECK", f"cannot open the PDF: {e}")
    pages: List[Dict[str, Any]] = []
    try:
        count = len(doc)
        if count == 0:
            raise SlideSyncError("INVALID_DECK", "the PDF has no pages")
        scale = dpi / 72.0
        for i in range(count):
            page = doc[i]
            try:
                w_pt, h_pt = page.get_size()
                if math.ceil(w_pt * scale) * math.ceil(h_pt * scale) > MAX_PAGE_PIXELS:
                    raise SlideSyncError("INVALID_ARGUMENT", f"page {i + 1} would be larger than {MAX_PAGE_PIXELS // 1_000_000} MP "
                                         f"at {dpi:g} dpi; lower --dpi")
                textpage = page.get_textpage()
                try:
                    raw_text = textpage.get_text_range()
                finally:
                    textpage.close()
                bitmap = page.render(scale=scale, fill_color=(255, 255, 255, 255), may_draw_forms=False,
                                     rev_byteorder=True, force_bitmap_format=pdfium.raw.FPDFBitmap_BGR)
                try:
                    png = png_bytes(bitmap.width, bitmap.height, bytes(bitmap.buffer), bitmap.stride, bitmap.n_channels)
                    size = (bitmap.width, bitmap.height)
                finally:
                    bitmap.close()
            finally:
                page.close()
            pages.append({"width_pt": round(float(w_pt), 3), "height_pt": round(float(h_pt), 3), "width_px": size[0],
                          "height_px": size[1], "text": normalise_text(raw_text), "png": png})
    finally:
        doc.close()
    return pages, _pdfium_versions(pdfium)


def import_deck(deck: str, out_dir: str, *, dpi: float = DEFAULT_DPI, overwrite: bool = False, soffice: Optional[str] = None,
                timeout: float = 600.0, runner: Runner = subprocess_runner) -> Dict[str, Any]:
    """Render every page of `deck` to `out_dir/page-NNN.png`, write its text to `out_dir/page-NNN.txt`, and the
    manifest to `out_dir/deck.json`. Only those names are written, directly inside `out_dir`; the deck is never written."""
    if not os.path.isfile(deck):
        raise SlideSyncError("INPUT_NOT_FOUND", f"no such file: {deck}")
    ext = os.path.splitext(deck)[1].lower()
    if ext != PDF_EXT and ext not in OFFICE_EXTS:
        raise SlideSyncError("INVALID_ARGUMENT", f"unsupported deck type '{ext or '(none)'}': use .pdf, or "
                             + ", ".join(OFFICE_EXTS) + " with LibreOffice installed")
    if not (MIN_DPI <= dpi <= MAX_DPI):
        raise SlideSyncError("INVALID_ARGUMENT", f"--dpi must be in [{MIN_DPI:g}, {MAX_DPI:g}]")
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        raise SlideSyncError("INVALID_ARGUMENT", f"-o must be a directory: {out_dir}")
    _pdfium()  # fail on the missing extra before a slow LibreOffice conversion
    converted_by = None
    with tempfile.TemporaryDirectory(prefix="slide_sync_import_") as tmp:
        if ext == PDF_EXT:
            pdf = deck
        else:
            pdf, _ = convert_to_pdf(deck, tmp, soffice=soffice, timeout=timeout, runner=runner)
            converted_by = {"tool": "libreoffice", "argv": ["soffice", "--headless", "--convert-to", "pdf"]}
        pages, versions = _render_pdf(pdf, dpi)
    names = [_page_name(i + 1, len(pages)) for i in range(len(pages))]
    targets = [os.path.join(out_dir, MANIFEST)] + [os.path.join(out_dir, n + e) for n in names for e in (".png", ".txt")]
    keep = {os.path.basename(t) for t in targets}
    stale = sorted(f for f in os.listdir(out_dir) if PAGE_FILE.match(f) and f not in keep) if os.path.isdir(out_dir) else []
    clash = [t for t in targets if os.path.lexists(t)]
    if (clash or stale) and not overwrite:
        first = (clash or [os.path.join(out_dir, stale[0])])[0]
        more = len(clash) + len(stale) - 1
        raise SlideSyncError("INVALID_ARGUMENT", f"refusing to overwrite {first}" + (f" and {more} more" if more else "")
                             + ": pass --overwrite or choose an empty directory")
    os.makedirs(out_dir, exist_ok=True)
    # With --overwrite: pages of an earlier, longer import go, and every target is unlinked before it is written, so a
    # symlink planted at a target name is replaced, never followed out of the directory.
    for f in stale + [os.path.basename(t) for t in clash]:
        os.remove(os.path.join(out_dir, f))
    out_pages = []
    for i, (name, p) in enumerate(zip(names, pages)):
        with open(os.path.join(out_dir, name + ".png"), "wb") as fh:
            fh.write(p["png"])
        with open(os.path.join(out_dir, name + ".txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(p["text"] + ("\n" if p["text"] else ""))
        out_pages.append({"index": i + 1, "image": name + ".png", "text_file": name + ".txt",
                          "width_px": p["width_px"], "height_px": p["height_px"],
                          "width_pt": p["width_pt"], "height_pt": p["height_pt"],
                          "image_sha256": hashlib.sha256(p["png"]).hexdigest(), "chars": len(p["text"]), "text": p["text"]})
    manifest = {
        "schema": DECK_SCHEMA,
        "source": {"path": os.path.abspath(deck), "format": ext[1:], "sha256": _sha256_file(deck), "converted_by": converted_by},
        "renderer": {"skill": "slide-sync", "skill_version": __version__, "engine": "pdfium", **versions, "dpi": dpi,
                     "background": "white", "png": "8-bit RGB, filter 0, zlib level 9, no metadata",
                     "text": "PDFium text layer (no OCR)", "text_normalisation": TEXT_NORMALISATION},
        "page_count": len(out_pages),
        "pages": out_pages,
        "provenance": "OBSERVED",
    }
    with open(os.path.join(out_dir, MANIFEST), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return manifest
