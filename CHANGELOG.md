# Changelog

## Unreleased

- Accept ffmpeg-skill 3.x: the contract's dependency range is `>=2.0.0 <4.0.0`. The tools and JSON keys used did not change in 3.0.0; `insert` always cuts with `--accurate` and re-encodes in `join`, so 3.0's `cut` defaults do not change its output (ADR-004). ffmpeg-skill 3.x needs Python 3.10+ and FFmpeg 6.1+; this skill keeps Python 3.9 for use with ffmpeg-skill 2.x.
- CI: the integration job runs against ffmpeg-skill `v2.24.1` and `v3.0.0` (tags, not `main`).
