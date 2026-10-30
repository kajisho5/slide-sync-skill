---
name: slide-sync-skill
description: Find where the slides change in a talk, lecture or conference recording and turn that into chapters - a slide timeline (start and length of every slide), a contact sheet to check it, chapter text for a YouTube description, and a copy of the video with container chapters. Also imports the speaker's deck (PDF, or PPTX / KEY via LibreOffice) as one PNG and one text per page, Japanese included, and matches each slide of the recording to the deck page on screen (any order, with a confidence, null when unsure). Splices a clip (a demo video, a slide's embedded video, a still) in at a slide boundary, with the chapters shifted. Use when asked to add chapters to a session recording, split a talk by slide, find slide changes, make a table of contents for a conference video, get the pages and text of a slide deck, find which page of the deck is shown when, or insert a demo video between two slides. Runs on ffmpeg-skill; local, no network.
---

# slide-sync-skill

Run `slide-sync <command> ... --json` (installed by `pip install -e .`). Every command prints one JSON document. It needs ffmpeg-skill (found through `--ffmpeg-skill DIR`, `$FFMPEG_SKILL_DIR`, `~/.claude/skills/ffmpeg-skill` or `../ffmpeg-skill`); `slide-sync doctor --json` says whether it is ready.

| Task | Command |
|---|---|
| Slide changes, with a sheet to check them | `slide-sync detect talk.mp4 --sheet sheet.png -o timeline.json --json` |
| Chapter text for a YouTube description | `slide-sync chapters talk.mp4 --timeline timeline.json --min-chapter 10 --chapters-out chapters.txt --json` |
| Same, with titles from a list (one per slide) | add `--titles titles.txt` |
| A copy of the video with chapters inside | add `-o talk_chapters.mp4` (streams copied, nothing re-encoded) |
| Pages of the deck as images and text | `slide-sync import deck.pdf -o pages/ --json` (needs `pip install -e '.[pdf]'`; `.pptx` / `.key` / `.odp` also need LibreOffice) |
| Which deck page is on screen when | `slide-sync match talk.mp4 --deck pages/ --timeline timeline.json -o match.json --json` (after `import` and `detect`) |
| A demo video after slide 3 (chapters shifted) | `slide-sync insert talk.mp4 --timeline timeline.json --insert 3=demo.mp4 --insert-title Demo -o talk_demo.mp4 --timeline-out talk_demo.json --json` (an image is a still of `--still-duration` s) |
| What this skill declares | `slide-sync contract --json`, `slide-sync doctor --json` |

## How to use it well

1. Run `detect` with `--sheet` first and look at the sheet: one thumbnail per detected slide.
2. Too many slides (animated builds, a video inside a slide, the camera cutting away)? Raise `--min-dwell` (seconds a slide must stay, default 2) or `--threshold` (cut strength, default 8). Too few (similar-looking slides)? Lower `--threshold`.
3. To know which page each slide is, `import` the deck and run `match` with the saved timeline. Segments with `"mixed": true` hold more than one page: `detect` missed a change there.
4. Then `chapters` from the saved timeline. For YouTube use `--min-chapter 10`: YouTube needs at least 3 chapters, the first at 0:00, each at least 10 s, and the JSON `warnings` say when that is not met.

## Report it honestly

- The timeline is **measured** picture cuts that stayed up at least `min_dwell` seconds, not an understanding of the slides. Say how many slides were found and that the sheet is the check.
- A recording of a projector by a camera (moving, re-framed, people in front) gives cuts that are not slide changes. Say so instead of presenting them as slides.
- Titles come from `--titles` in order; without it chapters are named `Slide N`. Do not invent slide titles.
- `-o` writes a new file; the input is never changed, and an existing output is refused without `--overwrite`.
- `import` text is the deck's own text layer, not OCR. An empty `text` means the page has none (a scanned or image-only slide); say so rather than guessing its content. A PPTX / KEY is rendered by LibreOffice, which substitutes fonts it lacks: tell the user to check the PNGs, or to export the PDF themselves.
- `match` gives each segment a page only above a confidence of 0.7 and a 0.1 lead over the next page; otherwise `page` is `null` with the `reason`. Report a `null` as "not matched" (a slide not in the deck, a blank, two identical pages, something in front), never fill it in from the neighbouring segments or the deck order. Say `confidence` and `view` (`located` means the slide was found inside a camera shot and straightened).
- `insert` places clips **between** slides only (after slide N; 0 = at the start) and re-encodes the talk through ffmpeg-skill. Every later slide and chapter moves by the clip's measured length; the `--timeline-out` file says so (`provenance: DERIVED`, `edits`, `render.drift`). Report the warnings it gives, and do not describe the result as the original recording.
- `DEPENDENCY_MISSING` (exit 3) means pypdfium2 or LibreOffice is not installed: pass on its install line, or ask for the deck as PDF.

Exit codes: `0` ok, `2` invalid input or arguments, `3` ffmpeg-skill, pypdfium2 or LibreOffice missing, `4` ffmpeg-skill or LibreOffice failed.

## Not this skill's job

Reading slide text from pixels (OCR: detection-skill), burning chapter cards into the picture (ffmpeg-skill `graphics --template chapter`), cutting the recording or clips inside a slide (video-editing-skill), uploading (publish-skill).
