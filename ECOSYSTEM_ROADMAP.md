# Roadmap — slide-sync-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **認識 (Recognition)**, status `active` since 0.1.0.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

0.1.0: `detect` (slide timeline from ffmpeg-skill scenes, min-dwell filter, contact sheet) and `chapters` (chapter text, container chapters through ffmpeg-skill metadata), with contract and doctor. Stdlib only; unit tests plus integration tests on the real engine.

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| スライド取込 | missing | PDF/PPTX -> page images + text (pypdfium2 or poppler; PyMuPDF is AGPL; LibreOffice for PPTX -- to verify) |
| スライド切替検出 | done | 0.1.0: ffmpeg-skill scenes + min-dwell; frame-hash refinement not needed on the fixtures, revisit with real recordings |

## Wiring

ffmpeg-skill `scenes` (detection, contact sheet) and `metadata` (chapters). No agent adapter yet (step 6).

## Steps

- [x] **1. Skeleton + contract/doctor + slide-timeline schema** (0.1.0)
  - Done when: OS static checks PASS
- [ ] **2. IMPORT op** — PDF -> PNG + text; PPTX via LibreOffice if present
  - Done when: page count + text fixture
- [x] **3. DETECT_CHANGES op** — screen recording -> change times (0.1.0, `detect`)
  - Done when: synthetic slideshow, +/-1 frame
- [ ] **4. MATCH op** — changes -> slide index
  - Done when: shuffled-slides fixture
- [ ] **5. Chapters / insert clips** — via ffmpeg-skill metadata and insert (chapters done in 0.1.0; insert clips remains)
  - Done when: chapters in the output
- [ ] **6. SKILL.md, CI; (agent side) adapter** (SKILL.md and CI done in 0.1.0; the agent adapter remains)

## Boundaries

Slide text OCR comes from detection-skill, not a second OCR.

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (42 tools, treated as complete). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
