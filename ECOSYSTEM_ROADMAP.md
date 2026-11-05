# Roadmap — slide-sync-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **認識 (Recognition)**, status `active` since 0.1.0.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

0.1.0: `detect` (slide timeline from ffmpeg-skill scenes, min-dwell filter, contact sheet) and `chapters` (chapter text, container chapters through ffmpeg-skill metadata), with contract and doctor. Stdlib only; unit tests plus integration tests on the real engine.
Since then: `import` (deck -> per-page PNG + text; pypdfium2 as the optional `pdf` extra, LibreOffice for PPTX / KEY; ADR-001 in `docs/decisions.md`) and `match` (each detected slide -> deck page, confidence, or null; image correlation with a located, de-keystoned slide, standard library; ADR-002) and `insert` (clips spliced in at slide boundaries through ffmpeg-skill cut / insert / join, the timeline and chapters shifted by the clips' measured lengths, provenance DERIVED; ADR-003).

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| スライド取込 | done | `import`: PDF -> page PNGs + text through pypdfium2 (Apache-2.0 OR BSD-3-Clause; PyMuPDF AGPL and Poppler GPL rejected); PPTX / KEY / ODP via LibreOffice when installed ([#4](https://github.com/kajisho5/slide-sync-skill/pull/4)) |
| スライド切替検出 | done | 0.1.0: ffmpeg-skill scenes + min-dwell; frame-hash refinement not needed on the fixtures, revisit with real recordings |

## Wiring

ffmpeg-skill `scenes` (detection, contact sheet), `metadata` (chapters), `look` (frames for match), and `probe` / `cut` / `insert` / `join` (insert clips); dependency `>=2.0.0 <4.0.0` (ffmpeg-skill 3.x accepted in [#8](https://github.com/kajisho5/slide-sync-skill/pull/8), ADR-004). Import uses pypdfium2 (optional extra `pdf`) and, for non-PDF decks, a system LibreOffice; neither goes through ffmpeg-skill. The agent measures `detect` (with a deck, `import` + `match`) as `slide_timeline` (step 6, video-production-agent#106); chapters and inserts are not planned from it yet.

## Steps

- [x] **1. Skeleton + contract/doctor + slide-timeline schema** (0.1.0)
  - Done when: OS static checks PASS
- [x] **2. IMPORT op** — PDF -> PNG + text; PPTX via LibreOffice if present ([#4](https://github.com/kajisho5/slide-sync-skill/pull/4), `import`)
  - Done when: page count + text fixture
- [x] **3. DETECT_CHANGES op** — screen recording -> change times (0.1.0, `detect`)
  - Done when: synthetic slideshow, +/-1 frame
- [x] **4. MATCH op** — changes -> slide index ([#5](https://github.com/kajisho5/slide-sync-skill/pull/5), `match`; ADR-002)
  - Done when: shuffled-slides fixture
- [x] **5. Chapters / insert clips** — via ffmpeg-skill metadata and insert (chapters in 0.1.0; insert clips in [#6](https://github.com/kajisho5/slide-sync-skill/pull/6), `insert`; ADR-003)
  - Done when: chapters in the output
- [x] **6. SKILL.md, CI; (agent side) adapter** (skill side done in 0.1.0, checked in [#7](https://github.com/kajisho5/slide-sync-skill/pull/7): `SKILL.md` documents every CLI command (`detect`, `chapters`, `import`, `match`, `insert`, `contract`, `doctor`) with examples; `.github/workflows/tests.yml` (unit: Linux / macOS / Windows x Python 3.9 / 3.12 plus `slide-sync contract --json`; integration: ffmpeg-skill + ffmpeg + LibreOffice + pypdfium2, the slide tests must not skip, `doctor`); it checks out no OS repository. Agent side: [video-production-agent#106](https://github.com/kajisho5/video-production-agent/pull/106) (its ADR-091) measures `detect` (and, with the deck, `import` + `match`) as the analysis kind `slide_timeline` for `slides.detect=true`, as an OBSERVED Observation and SlideEvents; without slide-sync-skill the measurement is handed off)
- [ ] **7. Layout plan for slide talks** — from `match`, a layout per segment (slide full, speaker picture-in-picture on the slide, side by side, speaker full during Q&A) written as a plan for ffmpeg-skill `broll` / `overlay`, with the rule used per segment. Field review 2026-11-05 (Velisa ROADMAP, third round)
  - なぜ要るか：スライド収録と登壇者カメラを、セッションごとに手で組まずにアーカイブの画にできる
  - Done when: a fixture with slides then Q&A gives PiP for the slides and speaker full for the Q&A
- [ ] **8. Clean deck pages over the captured slide** — where `match` is confident, the captured slide area (located and de-keystoned already) is replaced by the deck page, sharp and at the output size, for the matched time; a low-confidence match keeps the capture
  - なぜ要るか：プロジェクターを撮った眠いスライドを、元の資料の鮮明な画に差し替えられる
  - Done when: a fixture's replaced frames have higher sharpness than the capture, and an unmatched span is left as captured
- [ ] **9. Slide changes from a recording in progress** — `detect --follow` on a recording still being written emits slide changes as they are found (JSON lines), so the chapters are ready when the recording closes
  - なぜ要るか：収録中に章立てを進め、終演直後に章付きアーカイブを出せる
  - Done when: a fixture appended in bursts yields the same changes as a batch run

## Boundaries

Slide text OCR comes from detection-skill, not a second OCR.

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (3.x, 42 tools, contract_version 2.0; treated as complete, proposals go to Velisa's ROADMAP). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
