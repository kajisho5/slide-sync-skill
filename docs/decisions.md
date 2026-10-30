# Architecture Decision Records

## ADR-001 Slide import: pypdfium2 for PDF, LibreOffice for PPTX / KEY, fixed 144 dpi

Checked 2026-10-30 against PyPI and the wheels listed under Sources.

### Requirements

`ECOSYSTEM_ROADMAP.md` step 2 (IMPORT op): a slide deck becomes one PNG and one text per page. The decks come from conference and academic-meeting talks, so the requirements are:

- **Commercial use.** The licence allows commercial use of the skill and of anything it produces. No AGPL, and no GPL library linked into the process.
- **Japanese text.** The text layer of a Japanese deck comes out as the right characters, not as mojibake or look-alike code points.
- **Deterministic.** The same deck, DPI and engine build give the same bytes.
- **Optional, like every engine in this ecosystem.** `detect` and `chapters` keep running on the standard library alone, so the PDF engine is an extra. A missing engine is a clear error (exit 3), and `doctor` lists it with its install line.
- **Offline.** Nothing is downloaded at run time.

### PDF: rendering and text

| Candidate | Licence | Notes | Verdict |
|---|---|---|---|
| **pypdfium2 (PDFium)** | pypdfium2 5.14.0 wheel metadata: "BSD-3-Clause, Apache-2.0, dependency licenses". PDFium itself is BSD-3-Clause; the wheel ships the licences of its build dependencies (FreeType, ICU, libjpeg-turbo, OpenJPEG, lcms, zlib, ...) | Self-contained wheels for Linux, macOS and Windows, Python >= 3.6. Renders and reads the text layer through one library, the engine Chrome uses. Tested here with 4.30.0 and 5.14.0 | **Chosen** (`pip install 'slide-sync-skill[pdf]'`) |
| PyMuPDF | PyPI: "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License" | Good renderer and text | Rejected: AGPL (or a paid licence) |
| Poppler (`pdftoppm`, `pdftotext`), also through pdf2image (MIT) or python-poppler (GPL-2.0) | Poppler is GPL-2.0 / GPL-3.0 | A system binary on Linux; Windows needs a separate build | Rejected: GPL, and a second system dependency |
| pdfminer.six | MIT | Text only, no rendering, so PNGs would still need another engine | Not needed: PDFium gives both |
| pypdf | BSD-3-Clause | Text only, no rendering | Not needed: PDFium gives both |
| Ghostscript | AGPL-3.0 or commercial | Renders | Rejected: AGPL |

PNG encoding uses no imaging library: `deck.py` writes the PDFium bitmap as a PNG with `zlib` and `struct` from the standard library. This keeps Pillow out and keeps the bytes under this skill's control (see Determinism).

### PPTX / PPT / KEY / ODP

| Candidate | Licence | Notes | Verdict |
|---|---|---|---|
| **LibreOffice `soffice --headless --convert-to pdf`** | MPL-2.0. It runs as a separate program, nothing is linked | Opens .pptx, .ppt, .odp and Keynote .key (through libetonyek). Its PDF export embeds the fonts with a ToUnicode map, so Japanese text survives (checked with a generated .pptx on LibreOffice 24.2) | **Chosen, when installed.** Otherwise `DEPENDENCY_MISSING` (exit 3) naming LibreOffice, and `doctor` says how to install it or to export the deck as PDF |
| python-pptx | MIT | Reads the XML, but has no renderer: no page images | Rejected: no images |
| Keynote / PowerPoint automation | Proprietary, macOS / Windows only | Not headless or not cross-platform | Rejected |

How the converter runs:

- LibreOffice gets a **copy** of the deck in a temporary directory, so nothing (not even its `.~lock` file) is written next to the input.
- It runs with a fresh profile (`-env:UserInstallation=<temp>`), so it neither touches the user's profile nor clashes with a running LibreOffice. It runs with argv only, never a shell, under `--timeout` (default 600 s).
- It is found through `--soffice`, `$SLIDE_SYNC_SOFFICE`, `soffice` / `libreoffice` on PATH, `/Applications/LibreOffice.app` on macOS, or `%ProgramFiles%\LibreOffice` on Windows.

### Decision

- **One op, `slide-sync import <deck> -o <dir>`.** It writes `page-NNN.png`, `page-NNN.txt` and `deck.json` (`slide-sync/deck@1`) directly inside `<dir>`, and nothing else. NNN is at least 3 digits.
  - Existing targets are refused unless `--overwrite`, as everywhere in this skill.
  - With `--overwrite`, page files left over from an earlier, longer import are removed. Every target is unlinked before it is written, so a symlink planted at a target name is replaced, never followed out of `<dir>`.
  - The deck is opened read-only.
- **Fixed DPI, default 144.**
  - 144 dpi is 2 px per point. A PowerPoint 16:9 slide (13.333 x 7.5 in = 960 x 540 pt) becomes 1920 x 1080 px, the size of the 1080p recordings step 4 will match against.
  - In general the size is `ceil(points x dpi / 72)`, the PDFium rule. LibreOffice writes a 16:9 slide as 960.0095 pt wide, so it becomes 1921 px.
  - `--dpi` takes 18 to 600. A page bitmap above 64 MP is refused (lower `--dpi`).
- **Determinism.** Pages are rendered onto opaque white with forms off. They are written as 8-bit RGB PNG, filter 0 on every row, zlib level 9, and no ancillary chunks (no time, no gamma, no text). The same pixels therefore give the same bytes.
  - `deck.json` has no clock time. It records the pypdfium2 and PDFium versions, because a different PDFium build may anti-alias differently.
  - It also records the sha256 of the deck and of every PNG.
  - A test imports the same deck twice and compares every file byte for byte.
- **Text is PDFium's text layer, normalised the same way every time.**
  - CRLF / CR become LF, and control characters other than LF and TAB are dropped.
  - Then Unicode NFC, so `か` + U+3099 becomes `が`.
  - Then NFKC on CJK radicals only (U+2E80 to U+2FDF). Decks exported from PowerPoint or Keynote on some systems map 日 to U+2F47 ⽇ (KANGXI RADICAL SUN), which looks the same but does not match a search for 日.
  - Then trailing spaces on each line and blank lines at both ends are removed.
  - Full-width Latin (`９５％`) and half-width katakana are kept as written. Full NFKC would rewrite them, and the deck's own spelling is the fact.
  - The rule is in `deck.json` (`renderer.text_normalisation`) and in the contract.
- **No OCR.** A page with no text layer (a scanned or image-only slide) gets empty text. Reading text from pixels is detection-skill's job (its ADR-001: Tesseract, shared with slide-sync).
- **Errors.**
  - `DEPENDENCY_MISSING` (exit 3): pypdfium2 or LibreOffice missing.
  - `INVALID_DECK` (exit 2): PDFium cannot open the file (not a PDF, damaged, password-protected).
  - `CONVERT_FAILED` (exit 4): LibreOffice exits non-zero or writes no PDF.
  - `CONVERT_TIMEOUT` (exit 4, retryable).
- **doctor.** `import.pdf` (pypdfium2: found, version, licence, install line) and `import.libreoffice` (found, path, install line), with `import.ready.pdf` and `import.ready.pptx_key_odp`.
  - A missing import engine is a `notes` entry, not a `problems` entry. `doctor`'s exit status stays about `detect` and `chapters`, which do not need it.
- **Fixtures are built at test time.** The test PDF is written by a few lines of Python. Its Japanese lines use a Type0 / Identity-H font with a ToUnicode map, the way an exported deck's embedded subset font carries them, and include the U+2F47 case. The test PPTX is a minimal OOXML zip. No binary fixture is committed.
- **CI.** The unit jobs install `.[pdf]` and fail if the import tests are skipped. The integration job also installs LibreOffice Impress and a Japanese font, and fails if the LibreOffice test is skipped.

### Follow-ups

- **Speaker notes.** LibreOffice can export notes pages. Notes are not read yet.
- **Animated builds.** One PDF page per slide. A slide whose build steps appear as separate cuts in the recording maps to one page (step 4, MATCH, has to allow that).
- **ecosystem.json.** The OS install line for this skill could add the `pdf` extra, as it does `all` for artifacts-skill. That file belongs to AI-video-production-OS.

### Sources

- [pypdfium2 on PyPI](https://pypi.org/project/pypdfium2/) (5.14.0; wheel metadata `License: BSD-3-Clause, Apache-2.0, dependency licenses`) and [pypdfium2-team/pypdfium2](https://github.com/pypdfium2-team/pypdfium2)
- [PDFium](https://pdfium.googlesource.com/pdfium/) (BSD-3-Clause)
- [PyMuPDF on PyPI](https://pypi.org/project/PyMuPDF/) ("Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License")
- [Poppler](https://poppler.freedesktop.org/) (GPL), [pdf2image](https://pypi.org/project/pdf2image/) (MIT wrapper of Poppler), [python-poppler](https://pypi.org/project/python-poppler/) (GPL-2.0)
- [pdfminer.six](https://pypi.org/project/pdfminer.six/) (MIT), [pypdf](https://pypi.org/project/pypdf/) (BSD-3-Clause)
- [LibreOffice licence](https://www.libreoffice.org/about-us/licenses/) (MPL-2.0) and [command-line conversion](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html)
