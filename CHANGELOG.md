# Changelog

## v0.1.1 - 2026-11-19

- ci: release on push to main (version from PR labels, tag and GitHub Release) (#18) (d06d7ce)
- Contract in the OS contract shape; contract/contract.json committed (0.1.1) (#17) (fba0122)
- docs(roadmap): add the ownership decisions of 2026-11-12 (#16) (8b45628)
- feat(detect): --follow, slide changes from a recording in progress (roadmap step 9) (#15) (7b32715)
- feat(clean): deck pages over the captured slide where match is confident (roadmap step 8) (#14) (5fe7d8e)
- feat(layout): layout plan for slide talks from match (roadmap step 7) (#13) (50320a9)
- docs(roadmap): no live-streaming context (out of scope) (#12) (4cb3ed4)
- docs(roadmap): field review steps 7-9 (2026-11-05) (#11) (a2e7e32)
- docs(roadmap): bring wiring up to date (2026-11-05 review) (#10) (c48d23d)
- Roadmap step 6: agent side landed (video-production-agent#106) (#9) (28d4cee)
- Accept ffmpeg-skill 3.x (>=2.0.0 <4.0.0); CI on v2.24.1 and v3.0.0 (#8) (0bb14c8)
- Roadmap step 6: record the skill side (SKILL.md, CI) as done (#7) (03a48a1)
- feat: slide insert - clips spliced in at slide boundaries, chapters shifted (roadmap step 5) (#6) (616faa8)
- feat: slide match - each detected slide to its deck page (roadmap step 4) (#5) (05ded7d)
- feat: slide import - deck to per-page PNG and text (roadmap step 2) (#4) (f35db74)
- docs: add app icon and wordmark (made with fresnel) (#3) (1cc6b23)
- feat: slide-sync-skill 0.1.0 - slide timeline and chapters from a talk recording (#2) (24e65da)
- Add ECOSYSTEM_ROADMAP.md: gaps, wiring and PR-sized steps from the 2026-10-30 ecosystem audit (#1) (eb0f733)
- Initial commit (b03fb24)

## 0.1.1 - 2026-11-19

- The contract follows the OS contract shape (Velisa `registry/capability_contract.schema.json`, Velisa ADR-018; ADR-008): `skill` is `{id, package, version}` instead of the package name as a string, and `dependencies` lists only ffmpeg-skill, with `skill_id` added (`id`, `kind`, `version_range`, `tools`, `tools_for` and `located_by` stay). pypdfium2 and LibreOffice, which are not Skills, move unchanged to `deck.requires`; `doctor` reports each under `import` as before. No command, option, output document, tool, capability id or error changed; `contract_version` stays 1.0.
- `contract/contract.json` is the contract committed as a file (`slide-sync contract --json`, one line, sorted keys), so any commit's contract is `git show <ref>:contract/contract.json`. `python contract/generate.py` regenerates it, and a unit test (`--check`) and CI fail when it is stale.
- Accept ffmpeg-skill 3.x: the contract's dependency range is `>=2.0.0 <4.0.0`. The tools and JSON keys used did not change in 3.0.0; `insert` always cuts with `--accurate` and re-encodes in `join`, so 3.0's `cut` defaults do not change its output (ADR-004). ffmpeg-skill 3.x needs Python 3.10+ and FFmpeg 6.1+; this skill keeps Python 3.9 for use with ffmpeg-skill 2.x.
- CI: the integration job runs against ffmpeg-skill `v2.24.1` and `v3.0.0` (tags, not `main`).
