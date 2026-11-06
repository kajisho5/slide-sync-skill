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

## ADR-003 Insert clips: splice at slide boundaries through ffmpeg-skill cut / insert / join, shift the timeline by measured lengths

Decided 2026-10-30, checked against ffmpeg-skill 2.21.0 (`scripts/_contract.py --json --static`).

### Requirements

`ECOSYSTEM_ROADMAP.md` step 5 ("Chapters / insert clips, via ffmpeg-skill metadata and insert"). Chapters were done in 0.1.0; this is the second half: put a clip (a demo video, the video a slide embeds, a still such as a deck page or a title card) into the talk at a slide boundary, and keep the slide timeline and chapters right. Done when the chapters are in the output.

- **ffmpeg-skill does the media work.** This skill never runs ffmpeg itself (`engine.py`, and a test checks it).
- **Consistent timeline.** Every slide after an insertion moves later by what was inserted; the chapters of the output say where each slide and each clip now starts.
- **Provenance.** The output's timeline says it is derived, from which measured timeline, by which edits, with which engine steps.
- **Input read-only**, existing outputs refused without `--overwrite`, as everywhere in this skill.

### What ffmpeg-skill offers

| Tool | What it does (contract) | Use here |
|---|---|---|
| `insert` | "Turn a still image into a silent, timed video clip": `--duration`, `--width` / `--height` (fill and centre-crop, never distorted), `--fps` | A still clip, at the recording's size and rate |
| `cut` | One span of a video; `--accurate` re-encodes for a frame-exact cut (the default stream copy snaps to keyframes) | The recording split at each boundary that gets a clip |
| `join` | Clips in order, normalising size (pad), frame rate and audio layout; `--transition none` butts them with the concat filter; a clip with no audio stream is joined over generated silence; every clip is trimmed to its probed duration | The parts and clips, in order |
| `probe` | Duration, size, frame rate | The recording's size / rate, each video clip's length |
| `metadata` | Container chapters, streams copied | The shifted chapters |

`insert` alone cannot put a video into another one: its input is a still image. So "insert a clip" is `cut` + `join`, with `insert` for stills. There is no ffmpeg-skill tool that splices at a time in one step (`broll` overlays, it does not lengthen the video; `render` executes plans of the same tools).

### Decision

- **One op, `slide-sync insert <video> --insert N=CLIP [...] -o <video>`.** `N` is a slide of the timeline; the clip goes after it (0: before slide 1). Repeat `--insert` for more clips; several at one boundary go in in the order given. An image extension (`png`, `jpg`, `jpeg`, `webp`, `bmp`, `tif`, `tiff`) is a still of `--still-duration` seconds (default 5); anything else is a video, used as it is.
  - A slide index, not a time: a boundary is where a slide ends, so a clip never lands inside a slide, and the timeline already knows where that is.
  - `--insert-title` names the clips' chapters in `--insert` order; without one, a clip's chapter is its file name (no invented titles).
- **Steps.** `probe` the recording (size, rate, length) -> `insert` each still at that size and rate / `probe` each video clip -> `cut --accurate` the recording at each boundary that gets a clip (none when the clips go only at the start or the end) -> `join --transition none --width W --height H --fps F` -> `metadata --chapters` into `-o`.
  - `--accurate`: the join re-encodes anyway, and a stream-copy cut would snap the boundary to a keyframe, seconds away in a screen recording.
  - The recording's size and rate are passed to `join` explicitly; its default is the first clip's, which is the inserted clip when one goes at the start.
  - The cost is two encodes of the talk (cut, then join) at ffmpeg-skill's default quality (CRF 18). A single-pass splice would need a filter graph built here, which this skill does not do.
- **Shifting.** A pure function, `shift_timeline(timeline, inserts)`: a slide moves by the sum of the durations inserted before it; each clip becomes a segment of its own, `kind: insert`, with its `title`; segments are renumbered 1..m and a slide keeps its `source_slide`, so `--titles` (one per slide of the recording) and `Slide N` still name the recording's slides. Shifting a shifted timeline keeps working (edits accumulate).
  - The shift is the clip's **measured** duration (ffmpeg-skill `probe`, or `insert`'s own probe), the same number `join` trims the clip to, not the requested `--still-duration`.
  - After the join, its measured length is compared with the predicted one (recording + clips) and recorded as `render.drift`; more than 1.5 frames is a warning. On the integration fixture it is 0.
- **Output.** The shifted timeline is still `slide-sync/timeline@1`, so `chapters` and `match` read it, with:
  - `provenance: DERIVED` and `derived_from` (path, duration, provenance and slide count of the measured timeline);
  - `edits`: per clip, `after_slide`, `at_source`, `at_output`, measured `duration`, the shift after it, `title`, the clip's path, kind (`video` / `still`) and sha256;
  - `render`: every ffmpeg-skill step (tool, requested and measured lengths, cut mode, join notes such as generated silence), the engine version, the drift.
  `--timeline-out` writes it, `--chapters-out` the chapter text; `--json` prints it.
- **Engine tools.** `probe`, `cut`, `insert`, `join`, `metadata` (`INSERT_TOOLS`) are checked by `insert` only (`ENGINE_TOOL_MISSING`, exit 3). `doctor` reports whether they are usable as a note, so a missing one does not fail detect / chapters / match.

### Evidence

- **Unit (`ShiftTimelineTests`, `InsertOpTests`).** Shifting after a slide, at the start, at the end and several at one boundary; a shifted timeline shifted again; chapters and titles after the shift; `min_chapter` merging a short clip; bad slide numbers and durations. With ffmpeg-skill faked: the order and arguments of every call (cut boundaries, `--accurate`, the still at the recording's size and rate, the join order, `--transition none`), the chapter file handed to `metadata`, no cut when clips go only at the ends, the drift warning, every refusal before anything runs.
- **Integration (`IntegrationTests.test_inserted_clips_shift_the_slides_and_chapters`, real ffmpeg-skill).** Three 4 s slides at 320 x 180 / 25 fps; a 2 s 320 x 240 / 30 fps demo with sound after slide 1 and a 640 x 360 still for 1.5 s after slide 2. The output's container chapters are at 0, 4, 6, 10, 11.5 s within one frame, the output is 15.5 s, and the frame in the middle of every segment of the shifted timeline has that segment's colour.

### Limits

- The talk is re-encoded twice. For a final master, keep the original and treat this output as a delivery copy.
- A clip of another aspect ratio is padded (join's default `--fit pad`), not cropped.
- Clips go only at slide boundaries. A clip inside a slide, a cross-fade, or cutting parts of the talk out is editing: video-editing-skill.
- `match` on the output gives the inserted segments `page: null` (they are not deck pages), as it should.

## ADR-004 ffmpeg-skill 3.x accepted (`>=2.0.0 <4.0.0`), with 3.0's `cut` defaults

Decided 2026-10-31, checked against ffmpeg-skill v3.0.0 (CHANGELOG "3.0.0", `docs/contract.md` "What 3.0 changed", `contract --json` `removed`).

### What 3.0.0 changed, and what reaches this skill

| 3.0.0 change | Reaches this skill? |
|---|---|
| Python 3.9 and FFmpeg 5.x no longer supported | Only the environment: ffmpeg-skill's scripts run under this skill's interpreter, so a 3.x checkout needs Python 3.10+ and FFmpeg 6.1+. ffmpeg-skill 3.x refuses an older FFmpeg itself (`missing_tool`, `ffmpeg_unsupported`; here `ENGINE_FAILED` with its message), and its own `doctor` reports an old Python or FFmpeg as `ok: false`, which `slide-sync doctor` passes on |
| `cut`: edit list kept on a stream copy; `--vfr-guard sampled` | No: `insert` always cuts with `--accurate` (a re-encode), and the guard and the edit list are about stream copies |
| `cut`: an SDR HEVC source re-encodes as HEVC (2.x: x264) | Only the intermediate parts: `join --transition none --width --height --fps` re-encodes every part and clip with its own encoder, so the output is the same either way |
| `cut`: `precision`, `keyframe_snapped` meanings | No: `insert` reads `mode`, `probe.duration` and `output_duration` only |
| `probe`: `timecode` added | No (additive) |

### Decision

- **Range `>=2.0.0 <4.0.0`** in the contract. No 2.x flags (`--no-edit-list`, `--no-keep-hevc`, `--vfr-guard average`) are passed: none changes what `insert` produces, and passing them only for 3.x would add a version branch for nothing.
- **Python 3.9 stays** (`requires-python >=3.9`) for use with ffmpeg-skill 2.x; with 3.x the environment needs Python 3.10+, which ffmpeg-skill's own doctor reports.
- **CI** runs the integration job (real ffmpeg-skill, import, match and insert tests, none may skip) against the tags `v2.24.1` and `v3.0.0` instead of `main`. The unit jobs keep Python 3.9 / 3.12 with ffmpeg-skill faked.

### Evidence

All 56 tests pass locally against ffmpeg-skill v3.0.0 and v2.24.1 (Python 3.11, FFmpeg 6.1.1), including `IntegrationTests.test_inserted_clips_shift_the_slides_and_chapters` (chapters at 0, 4, 6, 10, 11.5 s; every segment's colour).

## ADR-005 Slide-talk layout: one layout and one named rule per segment, as ffmpeg-skill overlay / broll calls

Decided 2026-11-06, checked against ffmpeg-skill v2.24.1 and v3.0.0 (`overlay.py`, `broll.py`).

### Requirements

`ECOSYSTEM_ROADMAP.md` step 7: from `match`, a layout per segment (slide full, the speaker picture-in-picture on the slide, side by side, the speaker full during Q&A), written as a plan for ffmpeg-skill `broll` / `overlay`, with the rule used per segment. Done when a fixture with slides then Q&A gives PiP for the slides and the speaker full for the Q&A.

- **Recorded files only.** The slide recording and the speaker camera are files; nothing here switches live.
- **Explainable.** Every piece of the recording says which rule chose its layout, in words (`why`), so a wrong frame can be traced to a rule and overridden by an option, not by editing the plan.
- **ffmpeg-skill does the media work**, as everywhere in this skill; the plan is the list of its calls.

### What ffmpeg-skill offers

| Tool | What it does | Use here |
|---|---|---|
| `overlay --video <layer> --place SRC_START-SRC_END@AT ...` (2.13.0 and later) | The layer shown only in the placed ranges, each range of the layer re-timed to start at AT; the main picture untouched outside them; main audio kept | Picture-in-picture and side by side: one call per corner / band size, one `--place` per span |
| `broll <A> --insert <B> --at --end --from` (repeatable) | B over A for each window, A's timeline and (with `--audio a`) its audio unchanged | Speaker full: one call, one cutaway per span |
| `grid`, `multicam` | Whole-clip composites / camera switching | Not used: no time ranges (grid), its own sync and switching (multicam) |

There is no ffmpeg-skill tool that shrinks the main picture for part of the video, so a "true" side by side (slide moved left, camera right, both scaled) would need a filter graph built here, which this skill does not do.

### Decision

- **One op, `slide-sync layout <video> --match <json> | --deck <dir> [--camera <video>] ...`.** Without `-o` it prints the plan (`slide-sync/layout@1`; `--plan-out` writes it); with `-o` it also runs the plan. The recording and the camera are probed through ffmpeg-skill `probe` (size, length); nothing else is read.
- **Layouts:** `slide_full`, `slide_pip`, `side_by_side`, `speaker_full`.
- **Rules,** first that applies, per piece of a segment (segments are split at `--qa-from` and at the edges of the camera's coverage, so every piece has one layout):

  | Rule | When | Layout |
  |---|---|---|
  | `qa_from` | at or after `--qa-from` | speaker full |
  | `no_deck_page` | no segment of the recording matched any page | speaker full (and a warning) |
  | `before_first_deck_page` | an unmatched segment before the first matched one (the chair's introduction) | speaker full |
  | `after_last_deck_page` | an unmatched segment after the last matched one (Q&A on a blank or a slide not in the deck) | speaker full |
  | `unmatched_speaker` | an unmatched segment between matched ones, with `--unmatched speaker` | speaker full |
  | `unmatched_mid_talk_kept` | the same without it (default): it may be a demo or a backup slide, which the speaker full frame would hide | the slide layout (PiP) |
  | `deck_page_side_band` | a matched page whose `view` is `fit` (the page's aspect fitted in the frame) and leaves a free band of at least 12 % of the frame width at each side (`MIN_SIDE_BAND`; a 4:3 deck in a 16:9 capture leaves 12.5 %) | side by side, the camera scaled to the band's width in the right band, centred vertically |
  | `deck_page` | any other matched page (`--slides auto` or `pip`) | slide PiP |
  | `deck_page_no_side_band` | `--slides side` on a page with no band | slide PiP |
  | `slides_full` | `--slides full` | slide full |
  | `no_camera` | no `--camera` | slide full (the plan has no step) |
  | `camera_not_covering` | the camera does not cover this time (`--camera-offset`, its length) | slide full |

  A `located` view (a camera on the projection) gives no side band: where the slide sits in the frame is known only as a quadrilateral in match's grid, not to the pixel. `ambiguous` and `below_min_score` are not told apart: either way no page was claimed.
- **Spans and steps.** Consecutive pieces with one layout become a span. Steps in order: one `overlay` for all PiP spans (`--position <corner> --scale-percent <w>`), one per side-band width (`--position X,Y --margin 0 --scale <band>`), then one `broll` (`--audio a`) for all speaker-full spans. Each step reads the previous one's output; intermediates are `<output>.layout-stepN.<ext>`, refused if they already exist and removed after the run. Camera time is recording time plus `--camera-offset`, in `--place` and `--from`.
- **Audio.** The recording's, throughout (`overlay` keeps the main audio; `broll --audio a`). The speaker camera's own sound is not used.
- **Version.** `--place` came in ffmpeg-skill 2.13.0: the plan warns and rendering refuses (`ENGINE_TOOL_MISSING`) on an older one. The contract's range stays `>=2.0.0 <4.0.0` (ADR-004) for the other commands.
- **Provenance.** The plan is DERIVED from the match (`derived_from`: its path, source, deck, segment and matched counts).

### Evidence

- **Unit (`LayoutUnitTests`).** Every rule above on faked match documents; `--qa-from` splitting a segment; a camera that starts 2 s late and ends early (coverage, offsets in `--place` / `--from`); a 4:3 page pillarboxed in 1920 x 1080 (`side_by_side`, a 240 px band, the 16:9 camera at `1680,472`) against a 16:9 page (PiP); the exact `overlay` / `broll` arguments; rendering in order with intermediates removed, `--overwrite`, an ffmpeg-skill older than 2.13.0 refused; argument refusals; the CLI.
- **Done-when fixture (`SlideTalkLayoutTests`, real ffmpeg-skill v2.24.1 and v3.0.0).** A three-page PDF imported for real, shown 4 s per page and then 6 s of a blanked projector (Q&A), with sound; a green speaker camera. `layout --deck --camera -o` gives `slide_pip` (`deck_page`) for pages 1 to 3 and `speaker_full` (`after_last_deck_page`) for the Q&A; the output is 18 s with the recording's audio, the PiP corner is the camera's green and the rest of the slide its page colour on every slide, and the whole frame is green in the Q&A. No intermediate file is left.

### Limits

- **Q&A with the last slide still up** looks like the talk: the rules cannot see it. `--qa-from` says when the talk ended.
- **Sync** is given, not measured (`--camera-offset`).
- **Side by side** uses only a free band beside a pillarboxed slide; the slide is never moved or shrunk.
- **Re-encoding.** Each step re-encodes (at ffmpeg-skill's default quality): at most one `overlay` per corner / band and one `broll`.

## ADR-006 Clean deck pages: the page clip over the capture where match is confident (0.8), through ffmpeg-skill insert / broll

Decided 2026-11-06, checked against ffmpeg-skill v2.24.1 and v3.0.0 (`insert.py`, `broll.py`).

### Requirements

`ECOSYSTEM_ROADMAP.md` step 8: where `match` is confident, the captured slide area (located and de-keystoned already) is replaced by the deck page, sharp and at the output size, for the matched time; a low-confidence match keeps the capture. Done when a fixture's replaced frames have higher sharpness than the capture, and an unmatched span is left as captured.

- **Never a wrong page.** Showing a page the speaker did not show is worse than a soft picture, so the bar is above match's own (0.7), and any doubt keeps the capture.
- **The page as the output.** `match` has already found and straightened the slide to compare it; the clean picture is that slide, straightened, at the output size, which is the deck page itself, rendered from the imported page image (144 dpi by default: 1920 x 1080 for a 16:9 slide).
- **ffmpeg-skill does the media work.**

### Decision

- **One op, `slide-sync clean <video> --match <json> | --deck <dir> [--min-confidence 0.8] [--pad-color black] [--plan-out <json>] [-o <video>]`.** Without `-o` it prints the plan (`slide-sync/clean@1`); with `-o` it runs it (the same runner as `layout`: intermediates refused if present, removed afterwards).
- **Per segment, replace or keep, with the rule:**

  | Rule | When | Action |
  |---|---|---|
  | `confident_match` | a page, confidence at least `--min-confidence` (0.8), frames agree | replace |
  | `low_confidence` | a page, but below 0.8 | keep |
  | `mixed` | the segment's frames showed different pages (a missed change, a build) | keep |
  | `unmatched` | no page | keep |

  0.8 sits between match's 0.7 and the lowest right-page score measured in ADR-002 (0.86 on the camera fixtures; 0.866 on this step's fixture).
- **Steps.** Each replaced page becomes a still clip at the recording's frame rate, sized to fit the recording's frame with the page's aspect kept (`insert --duration <longest span + 0.1 s> --width --height --fps`); one `broll` puts the clips over their spans (`--insert <clip> --at --end --from 0`, repeated; `--audio a`, so the recording's sound is untouched; a page of another aspect is padded with `--pad-color`). Consecutive segments on one page are one span. The whole frame shows the page in a replaced span: in a camera shot the room around the projection goes too, which is the point of a clean slide.
- **Provenance.** DERIVED from the match; `rules` counts each rule, `replaced_count` the replaced segments.

### Evidence

- **Unit (`CleanUnitTests`).** Every rule; two segments of one page merged into one span; the page clip sizes (16:9 at 1280 x 720, 4:3 at 960 x 720 padded); the exact `insert` / `broll` arguments; `--min-confidence`; nothing to replace; a page missing from the deck; rendering in order with intermediates removed; the CLI.
- **Done-when fixture (`CleanPagesTests`, real ffmpeg-skill v2.24.1 and v3.0.0).** Three imported pages and a backup slide that is not in the deck, filmed by a simulated camera (dark surround, keystone, Gaussian blur sigma 1.6, sensor noise, CRF 24) at 960 x 540 with sound. `clean --deck -o` replaces the three pages (confidence 0.87 to 0.92) and keeps the backup slide (`unmatched`). Sharpness, the variance of the Laplacian at half resolution (2 x 2 means, so noise does not count as detail), is 3 to 4 times the capture's in the replaced frames (the test requires more than 2 times); the unmatched frames differ from the capture by under 4 grey levels on average (broll re-encodes them). Output length and sound are kept, no intermediate is left.

### Limits

- Only what is on the deck page shows: a laser pointer, annotations drawn live, or a video playing inside the slide are gone in a replaced span. Use `--min-confidence 1` (nothing replaced) or keep the capture for such talks; a follow-up could keep segments flagged by detection-skill.
- Animated builds: a build step matched to the full page shows the full page early.
- The talk is re-encoded once (broll), plus one short encode per page.

## ADR-007 Slide changes from a recording in progress: whole-prefix passes of ffmpeg-skill scenes, final at min_dwell + 1 s from the readable end

Decided 2026-11-06, checked against ffmpeg-skill v2.24.1 and v3.0.0 (`scenes.py`, `_common/probe.py` `detect_scenes`).

### Requirements

`ECOSYSTEM_ROADMAP.md` step 9: `detect --follow` on a recording still being written emits slide changes as they are found (JSON lines), so the chapters are ready when the recording closes. Done when a fixture appended in bursts yields the same changes as a batch run.

- **Recorded files, not a stream.** The input is a file a recorder is still writing, the same case as ingest-skill's `offload --follow` (its ADR-007). Nothing here reads a live feed.
- **The same answer as the batch run.** A change reported while recording must be one the finished file has; nothing is guessed early.
- **Slide-sync side only.** This skill reads the growing file through ffmpeg-skill as it stands. Copying it off the card or recorder as it grows is ingest-skill's.

### How ffmpeg-skill `scenes` decides, and why a prefix gives the same changes

`detect_scenes` scores every frame with scdet against the frame before it. A frame counts as a cut when three things hold: its score is above `--threshold`, it is more than 3 times the median of the +-12 frames around it, and it is the local maximum within +-2 frames. Cuts are then kept greedily, one at least `--min-scene` after the last kept one. At the end, a last cut closer than `--min-scene` to the duration is dropped.

So whether a cut at time t is kept depends on two things only: the frames up to 12 frames after t, and the cuts kept before t. Neither changes when more of the file is written. The one exception is the final drop, which applies only within `--min-scene` of the end. A cut at least `min_dwell` plus 12 frames before the end of the part that could be read is therefore final. This skill uses **min_dwell + 1 s** (12 frames is 0.5 s at 25 fps and 0.2 s at 60 fps).

`scenes` has no seek, and a window cut out through ffmpeg-skill `cut` would re-encode, or snap to a keyframe, and so change the scores. Each pass therefore reads the whole file as written so far. That costs one more read of the written part per pass. Passes run only when the file has grown, at most once per `--poll` (default 5 s).

### What can be read while it grows

Measured with ffmpeg-skill `scenes` / `probe` on the first 55 % of a file:

| Container as written while recording | Readable length | Result |
|---|---|---|
| MPEG-TS (`.ts`, `.m2ts`, `.mts`) | yes (11.0 s of 20) | changes as they come |
| Matroska written to a pipe (no duration element yet) | none | `waiting`: changes come at close |
| Matroska with its duration already written (a copy of a finished file) | the full length, wrong | not a growing file; the final pass is right |
| Fragmented MP4 (`empty_moov`) | ffprobe fails to read the header | `waiting` until it can be read |

A pass that ffmpeg-skill cannot read emits `waiting` once and is retried when the file grows. A pass with no measurable length reports nothing. The batch `detect` at close always runs.

### Decision

- **`slide-sync detect <video> --follow [--settle 30] [--poll 5] [--follow-for T] [--chapters-out txt] [-o timeline.json] --json`.** Same `--threshold` / `--min-dwell` as the batch run.
- **Events,** one JSON object per line:
  - `start`: the settings and the guard.
  - `pass`: size, the readable length, and "final up to".
  - `waiting`
  - `change`: `slide`, `time`, and `while_recording` (true when found during a pass, false when found only at close).
  - `retract`
  - `done`: `closed`, and the timeline.
- **Closed** means the file's size and mtime have been unchanged for `--settle` seconds (30 by default, as in ingest-skill). Whether the recorder still holds the file open is not detected, because there is no portable way to do it. Then one batch `detect` of the whole file gives the timeline, with a `follow` block: passes, how many changes were reported while recording and how many at close, and any retracted. Changes it has that were not reported yet are emitted then. A change reported earlier that it does not have is `retract`ed, never silently dropped.
- **`--follow-for T`** stops after T seconds even if the file still grows. The timeline then carries a warning that the recording may still be going.
- **Chapters ready at close.** `--chapters-out` is rewritten after every change (`Slide N` titles, written to a temporary file and renamed, so a reader never sees half of it) and once more from the final timeline. `-o` writes that timeline. Both refuse an existing file before the follow starts, unless `--overwrite` is given. `--sheet` is refused with `--follow`.

### Evidence

- **Unit (`FollowUnitTests`).** A file that grows by one burst per tick under a fake clock, with `scenes` faked to behave like the real one on a prefix:
  - every change reported while recording, and the same as the batch run;
  - a change within the guard of the end reported only at close;
  - a spurious cut seen in one pass retracted;
  - `--follow-for` before the file settles;
  - a file that never appears;
  - the guard rule itself;
  - the CLI's JSON lines, its chapter file and its timeline file.
- **Done-when fixture (`FollowIntegrationTests`, real ffmpeg-skill v2.24.1 / v3.0.0).** A 22 s MPEG-TS recording (six slides, sound) is appended to its file in ten bursts, 0.7 s apart, cut at arbitrary byte offsets. `detect --follow --poll 0.3 --settle 2.5` runs alongside. Its changes (4, 7, 12, 15, 19 s) equal the batch `detect` of the finished file, and the slides of its final timeline equal the batch slides. All five changes were emitted during passes, each once its time was 3 s clear of the readable end (the test requires at least three). Nothing was retracted, and the chapter file equals `chapters` of the batch timeline.

### Limits

- **Cost.** Every pass reads the whole written part. On a long recording, a larger `--poll` (or fewer passes) keeps it to a fraction of real time. The final pass is a full `detect`.
- **Containers.** Only those whose length ffmpeg-skill can measure while growing (MPEG-TS) report changes during recording. Others report them at close.
- **Sheet and match** are not run while recording: they are run on the closed file.
