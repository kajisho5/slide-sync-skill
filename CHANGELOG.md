# Changelog

## 0.1.1 - 2026-11-19

- The contract follows the OS contract shape (Velisa `registry/capability_contract.schema.json`, Velisa ADR-018; ADR-008): `skill` is `{id, package, version}` instead of the package name as a string, and `dependencies` lists only ffmpeg-skill, with `skill_id` added (`id`, `kind`, `version_range`, `tools`, `tools_for` and `located_by` stay). pypdfium2 and LibreOffice, which are not Skills, move unchanged to `deck.requires`; `doctor` reports each under `import` as before. No command, option, output document, tool, capability id or error changed; `contract_version` stays 1.0.
- `contract/contract.json` is the contract committed as a file (`slide-sync contract --json`, one line, sorted keys), so any commit's contract is `git show <ref>:contract/contract.json`. `python contract/generate.py` regenerates it, and a unit test (`--check`) and CI fail when it is stale.
- Accept ffmpeg-skill 3.x: the contract's dependency range is `>=2.0.0 <4.0.0`. The tools and JSON keys used did not change in 3.0.0; `insert` always cuts with `--accurate` and re-encodes in `join`, so 3.0's `cut` defaults do not change its output (ADR-004). ffmpeg-skill 3.x needs Python 3.10+ and FFmpeg 6.1+; this skill keeps Python 3.9 for use with ffmpeg-skill 2.x.
- CI: the integration job runs against ffmpeg-skill `v2.24.1` and `v3.0.0` (tags, not `main`).
