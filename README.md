<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/app-icon-dark.png">
    <img src="brand/app-icon.png" width="128" alt="slide-sync-skill app icon">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/logo/slide-sync-skill-wordmark-white.svg">
    <img src="brand/logo/slide-sync-skill-wordmark-black.svg" height="48" alt="slide-sync-skill">
  </picture>
</p>

<p align="center"><b>Slide changes in a talk, as a timeline and chapters</b></p>

Slide changes in a talk recording, as a slide timeline and chapters. Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS) (department 認識 / Recognition).

Typical use: a conference or lecture recording from the presenter's screen feed. `detect` finds where each slide starts. `chapters` turns that into the YouTube description's chapter list and, if you want, a copy of the video with chapters a player can seek by. `import` turns the speaker's deck (PDF, or PPTX / KEY through LibreOffice) into one image and one text per page. `match` says which page of that deck is on screen in each detected slide, in whatever order the speaker showed them. `insert` splices a clip (a demo video, the video a slide embeds, a still) in at a slide boundary, with the timeline and chapters shifted to match.

```bash
pip install -e .
export FFMPEG_SKILL_DIR=/path/to/ffmpeg-skill   # or ~/.claude/skills/ffmpeg-skill, ../ffmpeg-skill
slide-sync doctor
slide-sync detect talk.mp4 --sheet sheet.png -o timeline.json
slide-sync chapters talk.mp4 --timeline timeline.json --titles titles.txt --min-chapter 10 \
    --chapters-out chapters.txt -o talk_chapters.mp4

pip install -e '.[pdf]'                          # slide import (pypdfium2); PPTX / KEY also need LibreOffice
slide-sync import deck.pdf -o pages/             # pages/page-001.png, page-001.txt, ..., deck.json
slide-sync match talk.mp4 --deck pages/ --timeline timeline.json -o match.json
slide-sync insert talk.mp4 --timeline timeline.json --insert 3=demo.mp4 --insert 5=pages/page-006.png \
    --insert-title Demo --titles titles.txt -o talk_demo.mp4 --timeline-out talk_demo.json
```

`chapters.txt` (also in the JSON as `text`):

```
0:00 はじめに
2:15 背景
5:40 方法
```

## How it works

- **Detection.** [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) `scenes` finds picture cuts with ffmpeg's `scdet`. Cuts closer than `--min-dwell` seconds (default 2) are ignored, so a slide counts only if it stays up. `--threshold` (default 8, 0-100) sets how strong a cut must be. `--sheet` writes one thumbnail per detected slide, so you can check the result at a glance.
- **Chapters.** There is one chapter per slide, titled from `--titles` in order (then `Slide N`). `--min-chapter` merges short slides into the chapter before them. Warnings say when the list breaks YouTube's chapter rules: at least 3 chapters, the first at 0:00, each at least 10 s.
- **Writing.** ffmpeg-skill `metadata` writes the chapters into a copy (`-o`), with streams copied bit for bit. The input is never written. An existing output needs `--overwrite`.

- **Import.** [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (PDFium; Apache-2.0 OR BSD-3-Clause) renders every page of a PDF and reads its text layer. A `.pptx`, `.ppt`, `.key` or `.odp` deck is first converted to PDF by LibreOffice (`soffice --headless --convert-to pdf`, on a copy of the deck, with a throwaway profile); without LibreOffice that is a `DEPENDENCY_MISSING` error. Pages are rendered at a fixed **144 dpi** by default (`--dpi`, 18-600): a 16:9 PowerPoint slide (960 x 540 pt) becomes 1920 x 1080 px, in general `ceil(points x dpi / 72)`. Output is deterministic: 8-bit RGB PNG with no metadata, and the same text normalisation every time (line ends, Unicode NFC, Kangxi-radical look-alikes such as `⽇` folded to `日`). `deck.json` (`slide-sync/deck@1`) lists every page with its size, text and PNG sha256. Why these engines (PyMuPDF and Ghostscript are AGPL, Poppler is GPL): [docs/decisions.md](docs/decisions.md) ADR-001.

- **Match.** For each slide of the timeline, three frames (at 1/4, 1/2 and 3/4 of it) come from ffmpeg-skill `look`. Each frame and each page becomes a grid of mean brightness. A frame is compared as a whole (screen capture), with the page's aspect fitted (letterbox / pillarbox), and, when the slide sits inside a uniform surround (black bars, a dark room), as the quadrilateral found there, straightened (a camera's keystone, a small tilt). Brightness and contrast are normalised, the deck's average page is subtracted (so a shared template does not make every page look alike), and the score is the correlation, 0 to 1, median over the frames. A segment gets a page only when its score is at least **0.7** (`--min-score`) and at least **0.1** ahead of the next page (`--min-margin`); otherwise `page` is `null` with the reason (`below_min_score`, `ambiguous`). Frames of one slide that show different pages are flagged `mixed` (a change `detect` missed). Method, thresholds and what they were measured on: [docs/decisions.md](docs/decisions.md) ADR-002.

- **Insert.** `--insert N=CLIP` puts a clip after slide N of the timeline (0: before slide 1); repeat it for more. ffmpeg-skill does the media work: `probe` measures the recording and each video clip, `insert` turns an image into a still clip of `--still-duration` seconds (default 5) at the recording's size and frame rate, `cut --accurate` splits the recording at each boundary, `join --transition none` puts parts and clips together at the recording's size and rate (other sizes padded, a clip without sound gets silence), and `metadata` writes the chapters. Every slide after a clip moves later by the clip's measured length, and each clip gets a chapter of its own (`--insert-title`, or its file name). `--timeline-out` writes the shifted timeline: `slide-sync/timeline@1` with `provenance: DERIVED`, `derived_from`, `edits` (where each clip went, its length and sha256) and `render` (each ffmpeg-skill step and the measured drift from the predicted length). Why it works this way: [docs/decisions.md](docs/decisions.md) ADR-003.

This skill never runs ffmpeg or ffprobe itself: ffmpeg-skill is its only media boundary. `detect`, `chapters` and `match` need nothing beyond the Python standard library; `import` needs the optional `pdf` extra (and LibreOffice for non-PDF decks). Nothing makes network access.

## Commands

| Command | What it does |
|---|---|
| `detect <video> [--threshold 8] [--min-dwell 2] [--sheet png] [-o timeline.json]` | Slide timeline (`slide-sync/timeline@1`): slides with start, end and duration, change times, detector settings, engine version |
| `chapters <video> [--timeline json] [--titles txt] [--prefix Slide] [--min-chapter s] [--chapters-out txt] [-o video]` | Chapters from a timeline. Without `--timeline` it runs `detect` first |
| `import <deck> -o <dir> [--dpi 144] [--soffice path] [--overwrite]` | `<dir>/page-NNN.png`, `page-NNN.txt` and `deck.json` for a `.pdf`, or a `.pptx` / `.ppt` / `.key` / `.odp` when LibreOffice is installed. Only those names are written; existing ones need `--overwrite` |
| `match <video> --deck <dir> [--timeline json] [--min-score 0.7] [--min-margin 0.1] [--samples 3] [-o match.json]` | Match timeline (`slide-sync/match@1`): for every slide, start, end, deck `page` (or `null` and the `reason`), `confidence`, `margin` over the runner-up, `method`, `view`, per-frame pages, top candidates; `pages_shown`, `pages_not_shown`, `warnings`. Without `--timeline` it runs `detect` first |
| `insert <video> --insert N=CLIP [--insert ...] [--insert-title T ...] [--still-duration 5] [--timeline json] [--titles txt] [--prefix Slide] [--min-chapter s] [--chapters-out txt] [--timeline-out json] -o <video>` | A copy of the video with clips spliced in after slides N (0: at the start) and the shifted chapters written in. Without `--timeline` it runs `detect` first |
| `contract --json` | Machine-readable contract in the OS registry's shape |
| `doctor --json` | Python, ffmpeg-skill location and version, ffmpeg / ffprobe, and whether `scenes` / `metadata` / `look` are usable (and, for `insert`, `probe` / `cut` / `insert` / `join`, as a note); for import, pypdfium2 and LibreOffice with their install lines (a missing one is a note, not a failure) |

Every command prints one JSON document with `--json`. Exit codes: 0 ok, 2 invalid input or arguments (including a file PDFium cannot open, or a deck directory without `deck.json`), 3 ffmpeg-skill, pypdfium2 or LibreOffice missing (or ffmpeg-skill lacking a tool), 4 ffmpeg-skill or LibreOffice failed.

## Limits

- It works best on a screen feed (capture of the presentation PC, a switcher program out, a Zoom recording of a shared screen).
- A camera filming a projector also cuts on camera moves, people walking in front, and re-framing. Raise `--min-dwell`, and check the sheet.
- Animated builds and videos inside a slide can count as changes.
- `import` reads the text layer of the deck. A scanned or image-only page has no text layer and gets empty text; reading text from pixels (OCR) is detection-skill's job.
- PPTX / KEY rendering is LibreOffice's: fonts the deck uses but the machine lacks are substituted, so check the PNGs (or export the PDF from PowerPoint / Keynote yourself).
- `match` compares pictures, not text. A slide that is not in the imported deck, a blanked projector, two identical pages, a person standing in front of the slide, or a camera zoomed into part of it give `page: null`, by design. Something that crosses the slide's edge or a surround that is not uniform (stage lights, heads at the bottom of the frame) defeats the straightening; then only a near-full-frame shot still matches. Details and measurements: ADR-002.
- `insert` re-encodes the talk (twice: the cut and the join), pads clips of another aspect ratio, and places clips only between slides. Anything finer is editing (video-editing-skill).

## Tests

```bash
python -m unittest -v tests/test_slide_sync.py
```

The integration tests run the real ffmpeg-skill on generated slide decks. They are skipped when ffmpeg-skill or ffmpeg is not found. The import tests build their PDF (English and Japanese pages) and PPTX at test time; they need `pip install -e '.[pdf]'`, and the real-LibreOffice test also needs `soffice`. The match unit tests draw their pages and frames in Python. The shuffled-slides test imports a generated deck, films it with ffmpeg out of order (with a slide that is not in the deck and a blanked projector), as a screen capture and as a keystoned camera shot, and checks every segment's page; it needs ffmpeg-skill, ffmpeg and pypdfium2. The insert unit tests check the timeline shift and fake ffmpeg-skill; the insert integration test splices a generated demo clip and a still into a generated talk with the real ffmpeg-skill and checks the output's chapters and the picture in every segment.

## License

MIT
