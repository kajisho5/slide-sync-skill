"""Machine-readable contract (`slide-sync contract --json`), in the shape the AI Video Production OS registry reads
(skill_id, version, contract_version, provides, dependencies, not_provided)."""
from __future__ import annotations

from typing import Any, Dict

from . import __version__
from .core import DEFAULT_MIN_DWELL, DEFAULT_THRESHOLD, TIMELINE_SCHEMA
from .engine import ENV_DIRS, REQUIRED_TOOLS

SKILL_ID = "slide-sync"
CONTRACT_VERSION = "1.0"

TOOLS = [
    {"tool_id": "slide-sync/detect", "role": "measurement", "writes_media": False, "mutates_input": False, "deterministic": True,
     "description": "Slide changes of a talk recording: one entry per slide (start, end, duration), from ffmpeg-skill scenes",
     "argv": ["detect", "<video>", f"[--threshold {DEFAULT_THRESHOLD:g}]", f"[--min-dwell {DEFAULT_MIN_DWELL:g}]", "[--sheet <png>]",
              "[-o <timeline.json>]", "--json"]},
    {"tool_id": "slide-sync/chapters", "role": "execution", "writes_media": True, "mutates_input": False, "deterministic": True,
     "description": "Chapters from a slide timeline: chapter text (YouTube description format) and, with -o, a copy of the "
                    "video carrying them as container chapters (ffmpeg-skill metadata, streams copied)",
     "argv": ["chapters", "<video>", "[--timeline <timeline.json>]", "[--titles <titles.txt>]", "[--prefix Slide]",
              "[--min-chapter <s>]", "[--chapters-out <txt>]", "[-o <video>]", "[--overwrite]", "--json"]},
]

ERRORS = {
    "INPUT_NOT_FOUND": {"exit_code": 2, "retryable": False},
    "INVALID_ARGUMENT": {"exit_code": 2, "retryable": False},
    "INVALID_TIMELINE": {"exit_code": 2, "retryable": False},
    "OUTPUT_IS_INPUT": {"exit_code": 2, "retryable": False},
    "ENGINE_NOT_FOUND": {"exit_code": 3, "retryable": False},
    "ENGINE_TOOL_MISSING": {"exit_code": 3, "retryable": False},
    "ENGINE_FAILED": {"exit_code": 4, "retryable": False},
    "ENGINE_TIMEOUT": {"exit_code": 4, "retryable": True},
}


def contract() -> Dict[str, Any]:
    return {
        "skill_id": SKILL_ID,
        "id": SKILL_ID,
        "skill": "slide-sync-skill",
        "version": __version__,
        "contract_version": CONTRACT_VERSION,
        "provides": [{"id": "slides.detect_changes", "lifecycle": "EXPERIMENTAL", "tool_id": "slide-sync/detect"},
                     {"id": "slides.chapters", "lifecycle": "EXPERIMENTAL", "tool_id": "slide-sync/chapters"}],
        "dependencies": [{"id": "ffmpeg-skill", "kind": "skill", "required": True, "version_range": ">=2.0.0 <3.0.0",
                          "tools": list(REQUIRED_TOOLS), "located_by": ["--ffmpeg-skill"] + [f"${e}" for e in ENV_DIRS]
                          + ["~/.claude/skills/ffmpeg-skill", "./vendor/ffmpeg-skill", "../ffmpeg-skill"]}],
        "not_provided": ["slide import from PDF / PPTX (planned)", "matching a change to a slide of the deck (planned)",
                         "slide text OCR (detection-skill)", "running ffmpeg or ffprobe directly (only through ffmpeg-skill)",
                         "editing or re-encoding the picture", "network access of any kind"],
        "tools": TOOLS,
        "timeline": {"schema": TIMELINE_SCHEMA, "provenance": "OBSERVED"},
        "execution": {"canonical_invocation": ["slide-sync", "<command>", "...", "--json"], "network": False, "shell": False,
                      "engine": "ffmpeg-skill (scripts run with this Python, argv only)", "input_is_read_only": True,
                      "overwrites": "never unless --overwrite; -o equal to the input is refused"},
        "errors": ERRORS,
    }
