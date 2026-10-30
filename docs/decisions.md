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

## ADR-002 Slide match: luma-grid correlation with a located, de-keystoned slide; null below 0.7 or within 0.1

Measured 2026-10-30 on the fixtures listed under Evidence.

### Requirements

`ECOSYSTEM_ROADMAP.md` step 4 (MATCH op): for every segment of a `detect` timeline, say which page of an `import`ed deck is on screen. Done when a shuffled-slides fixture (pages shown back and forth, skipped, out of deck order) maps every segment to the right page.

- **Order-free.** Each segment is matched on its own picture. No assumption that pages come in deck order, once each, or at all.
- **Never invent.** When no page is clearly on screen (a backup slide that is not in the deck, a blanked projector, the camera on the speaker, two identical pages), the segment gets `page: null` and the reason, not the nearest guess.
- **Screen capture first, camera reasonably.** A capture of the presentation PC is the main case. A camera filming the projection adds a dark surround, scale, keystone, a small tilt, exposure and noise.
- **Deterministic, standard library, no OCR.** Like `detect` and `chapters`, matching needs nothing beyond Python. OCR stays with detection-skill (ADR-001), so this compares pictures only.
- **Frames through ffmpeg-skill.** This skill never runs ffmpeg itself (see `engine.py`).

### Method

| Step | What happens | Why |
|---|---|---|
| Frames | For each segment, `--samples` frames (default 3) at 1/4, 1/2, 3/4 of it, kept 0.1 s clear of its ends, all taken in one ffmpeg-skill `look --at ... --width 640 --no-timecode` call. `look` joins `REQUIRED_TOOLS` | Several frames give a median (a person walking past one frame does not decide); 640 px is 2.5 px per grid cell; one call keeps it to one probe |
| PNG | Read by `match.py` with `zlib` and `struct`: 8/16-bit, grey / RGB / palette / alpha, all five row filters | No Pillow, as in ADR-001 |
| Grid | Mean luma, `(R + G + B) / 3`, of each cell of a 256 x 144 grid over the whole picture | One C-level `sum` per cell and pixel row: a 1920 x 1080 page takes about 0.4 s in pure Python |
| Views of a frame | **full**: the whole frame. **fit**: the page's aspect ratio fitted and centred (letterbox / pillarbox), when the aspects differ by more than 1 %. **located**: the slide found inside a uniform surround, as a quadrilateral, warped back to a rectangle | The slide need not fill the frame; the best view wins |
| Locating | An edge whose outermost line is uniform (mean absolute deviation from its median at most 12 luma levels) is a surround. Lines of that colour are removed up to the slide. Along each line across the edge (the middle 70 %), the point where luma crosses halfway from the surround to the slide is a sub-cell edge point. A least-squares line through them (refitted without points more than 1.5 cells off) is the edge, the four lines meet at the corners, and Heckbert's square-to-quad projective map (1989) samples the slide as seen straight on (2 x 2 bilinear samples per feature cell) | A camera off the projector's axis sees a keystoned quadrilateral, not a box. A bounding box keeps dark wedges at the corners, and a single row of dark bar weighs more in the correlation than all of a white slide's text (measured: 0.94 with the exact box, 0.50 with one extra bar row) |
| Inset | Located and fitted views leave 3 % out at each edge and are compared with the pages' inset view (the same 3 % left out) | Absorbs edge error of a fraction of a cell |
| Feature | Each view resampled to 128 x 72 by area averaging, z-scored (brightness and contrast drop out), the deck's mean z-scored page subtracted (from 3 pages up), then made zero-mean unit-length | A deck shares a template: before subtracting the mean page, two different pages of the test deck correlated at 0.95. Z-scoring before the subtraction is what makes a washed-out projection (contrast 0.5) score 0.91 instead of 0.31 |
| Score | Correlation (dot product of the unit vectors), floored at 0, best over the frame's views. A segment's score for a page is the median over its frames | |
| Blank pictures | A picture whose luma standard deviation is under 2 is flat. Flat against flat scores by mean-colour distance (1 at equal, 0 at 64 levels apart); flat against content scores 0 | Correlation is undefined on a flat picture |
| Decision | The best page counts when its score is at least `--min-score` (0.7) **and** it leads the runner-up by at least `--min-margin` (0.1). Otherwise `page: null`, `reason: below_min_score` or `ambiguous` | "Never invent" |
| Per frame | The same rule on each frame alone gives `samples[].page`. Frames that disagree set `mixed: true` and a warning: the segment probably holds a change `detect` missed (or an animated build) | The median still decides the segment, but the disagreement is reported, not hidden |

Grid and feature sizes were chosen on the adversarial deck below (Evidence). At 128 x 72 / 32 x 18 a wrong page scored up to 0.80; at 256 x 144 / 128 x 72 up to 0.59, while the right page under the camera conditions stayed above 0.87.

### Output

`slide-sync match <video> --deck <dir> [--timeline json]` prints `slide-sync/match@1`: per segment `start`, `end`, `page` (deck index, or null), `confidence` (the score of the best page), `margin` (over the runner-up), `method` (`luma-grid-zncc`), `view` (`full`, `fit <aspect>`, `located`), `reason`, `samples` (time and per-frame page), `mixed`, and the top three `candidates`. Also `pages_shown`, `pages_not_shown`, `warnings` (unmatched or mixed segments, a timeline measured on another video) and the matcher settings. Without `--timeline` it runs `detect` first. Exit codes as elsewhere; an unreadable page image is `INVALID_IMAGE` (2), a directory without `deck.json` is `INVALID_DECK` (2).

### Evidence

Three fixtures, all generated at test time:

- **Adversarial deck (development only).** Ten white pages of one template: a title and two to five bullet lines that differ only in their words. Shown as a 1280 x 720 screen capture, letterboxed at 75 %, and filmed by two simulated cameras (dark room, keystone moving corners by up to 6 % of the frame, noise, blur, lowered contrast), plus a second deck of the same template that is *not* the one imported.

  | Measured on 24 frames, 4 conditions | Value |
  |---|---|
  | Lowest score of the right page | 0.871 (camera with blur, noise and contrast 0.85) |
  | Highest score of a wrong page | 0.593 |
  | Smallest lead of the right page | 0.31 |
  | Highest score of a same-template slide that is not in the deck | 0.422 |
  | Stronger keystone (corners moved by 7 % / 11 %), a 1.7 degree tilt, contrast 0.5 (one frame each) | 0.893, 0.902, 0.907 (right page), runner-up at most 0.43 |

  So 0.7 sits 0.1 above every wrong page and 0.17 below every right one, and a 0.1 margin is a third of the smallest lead measured.
- **Unit fixture (`MatchUnitTests`).** Pages drawn in Python (one template, different "text" blocks), frames drawn full-frame and inside a black surround, `look` faked. Shuffled order with repeats, a page that is not in the deck (0.46, null), a black frame (null), two identical pages (null, `ambiguous`), a segment that holds two pages (`mixed`), a synthetic keystoned quadrilateral found to within 0.5 cells, every PNG filter.
- **Done-when fixture (`ShuffledSlidesTests`).** A six-page PDF imported for real, shown by ffmpeg as 1 2 4 3 2 x 5 b 3 6 (x: a backup slide not in the deck, b: projector blanked), as a screen capture and as a keystoned, noisy camera shot. `detect` finds all ten segments within one frame; `match` gives `[1, 2, 4, 3, 2, null, 5, null, 3, 6]` on both. Right pages score 0.86 to 0.99, the backup slide 0.36 (screen) / 0.41 (camera), the blank under 0.05.

### Limits (documented, not handled)

- **Something in front of the slide** that crosses its edge (a person at the screen) breaks the located edge and darkens the picture: measured 0.16, so null. A person passing through one of three frames is outvoted by the median.
- **A zoomed camera** that shows only part of the slide, with no surround: measured 0.12, null. Only whole slides are matched.
- **Strong lens distortion, or a surround that is not uniform** (stage lights, a bright wall, audience heads on the edge): no slide is located, and the whole-frame view usually falls below 0.7, so null rather than wrong.
- **Animated builds.** One PDF page per slide; each build step correlates with the full page and usually clears 0.7, but an early step with little on it may not. Consecutive segments on the same page are reported as such.
- **Identical or near-identical pages** are `ambiguous` by design.
- **Speed.** Pure Python: about 0.4 s per page and 0.6 s per frame on a laptop-class CPU, plus one ffmpeg seek per frame. A 50-page deck and a 60-slide talk take a minute or two.
- **Text.** No OCR here. A follow-up can add slide text as a second signal (page text from `import` against frame text from detection-skill) when the two images are ambiguous.

### Follow-ups

- Text as a tie-breaker, once detection-skill offers OCR output in a shape this skill can read.
- A `chapters` option to title chapters from matched pages (a page's first text line), and merging consecutive segments on the same page.
