---
name: slide-sync-skill
description: Find where the slides change in a talk, lecture or conference recording and turn that into chapters - a slide timeline (start and length of every slide), a contact sheet to check it, chapter text for a YouTube description, and a copy of the video with container chapters. Also imports the speaker's deck (PDF, or PPTX / KEY via LibreOffice) as one PNG and one text per page, Japanese included. Use when asked to add chapters to a session recording, split a talk by slide, find slide changes, make a table of contents for a conference video, or get the pages and text of a slide deck. Runs on ffmpeg-skill; local, no network.
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
| What this skill declares | `slide-sync contract --json`, `slide-sync doctor --json` |

## How to use it well

1. Run `detect` with `--sheet` first and look at the sheet: one thumbnail per detected slide.
2. Too many slides (animated builds, a video inside a slide, the camera cutting away)? Raise `--min-dwell` (seconds a slide must stay, default 2) or `--threshold` (cut strength, default 8). Too few (similar-looking slides)? Lower `--threshold`.
3. Then `chapters` from the saved timeline. For YouTube use `--min-chapter 10`: YouTube needs at least 3 chapters, the first at 0:00, each at least 10 s, and the JSON `warnings` say when that is not met.

## Report it honestly

- The timeline is **measured** picture cuts that stayed up at least `min_dwell` seconds, not an understanding of the slides. Say how many slides were found and that the sheet is the check.
- A recording of a projector by a camera (moving, re-framed, people in front) gives cuts that are not slide changes. Say so instead of presenting them as slides.
- Titles come from `--titles` in order; without it chapters are named `Slide N`. Do not invent slide titles.
- `-o` writes a new file; the input is never changed, and an existing output is refused without `--overwrite`.
- `import` text is the deck's own text layer, not OCR. An empty `text` means the page has none (a scanned or image-only slide); say so rather than guessing its content. A PPTX / KEY is rendered by LibreOffice, which substitutes fonts it lacks: tell the user to check the PNGs, or to export the PDF themselves.
- `DEPENDENCY_MISSING` (exit 3) means pypdfium2 or LibreOffice is not installed: pass on its install line, or ask for the deck as PDF.

Exit codes: `0` ok, `2` invalid input or arguments, `3` ffmpeg-skill, pypdfium2 or LibreOffice missing, `4` ffmpeg-skill or LibreOffice failed.

## Not this skill's job

Reading slide text from pixels (OCR: detection-skill), matching changes to the deck's pages (planned here), burning chapter cards into the picture (ffmpeg-skill `graphics --template chapter`), cutting the recording (video-editing-skill), uploading (publish-skill).
