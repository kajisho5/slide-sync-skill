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

Typical use: a conference or lecture recording from the presenter's screen feed. `detect` finds where each slide starts. `chapters` turns that into the YouTube description's chapter list and, if you want, a copy of the video with chapters a player can seek by.

```bash
pip install -e .
export FFMPEG_SKILL_DIR=/path/to/ffmpeg-skill   # or ~/.claude/skills/ffmpeg-skill, ../ffmpeg-skill
slide-sync doctor
slide-sync detect talk.mp4 --sheet sheet.png -o timeline.json
slide-sync chapters talk.mp4 --timeline timeline.json --titles titles.txt --min-chapter 10 \
    --chapters-out chapters.txt -o talk_chapters.mp4
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

This skill never runs ffmpeg or ffprobe itself: ffmpeg-skill is its only media boundary. It has no dependencies beyond the Python standard library and makes no network access.

## Commands

| Command | What it does |
|---|---|
| `detect <video> [--threshold 8] [--min-dwell 2] [--sheet png] [-o timeline.json]` | Slide timeline (`slide-sync/timeline@1`): slides with start, end and duration, change times, detector settings, engine version |
| `chapters <video> [--timeline json] [--titles txt] [--prefix Slide] [--min-chapter s] [--chapters-out txt] [-o video]` | Chapters from a timeline. Without `--timeline` it runs `detect` first |
| `contract --json` | Machine-readable contract in the OS registry's shape |
| `doctor --json` | Python, ffmpeg-skill location and version, ffmpeg / ffprobe, and whether `scenes` / `metadata` are usable |

Every command prints one JSON document with `--json`. Exit codes: 0 ok, 2 invalid input or arguments, 3 ffmpeg-skill missing or lacking a tool, 4 ffmpeg-skill failed.

## Limits

- It works best on a screen feed (capture of the presentation PC, a switcher program out, a Zoom recording of a shared screen).
- A camera filming a projector also cuts on camera moves, people walking in front, and re-framing. Raise `--min-dwell`, and check the sheet.
- Animated builds and videos inside a slide can count as changes.
- Not yet: importing the PDF / PPTX deck and matching each change to a page. These are planned here (`ECOSYSTEM_ROADMAP.md`, steps 2 and 4). Reading slide text is detection-skill's job.

## Tests

```bash
python -m unittest -v tests/test_slide_sync.py
```

The integration tests run the real ffmpeg-skill on generated slide decks. They are skipped when ffmpeg-skill or ffmpeg is not found.

## License

MIT
