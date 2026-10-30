"""The only media boundary: ffmpeg-skill's own scripts, run as `python3 <ffmpeg-skill>/scripts/<tool>.py ... --json`.
This skill never runs ffmpeg or ffprobe itself and never builds a filter graph.

ffmpeg-skill is found, in order: --ffmpeg-skill, $SLIDE_SYNC_FFMPEG_SKILL_DIR, $FFMPEG_SKILL_DIR,
~/.claude/skills/ffmpeg-skill, ./vendor/ffmpeg-skill, ../ffmpeg-skill.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

ENV_DIRS = ("SLIDE_SYNC_FFMPEG_SKILL_DIR", "FFMPEG_SKILL_DIR")
REQUIRED_TOOLS = ("scenes", "metadata")

Runner = Callable[[List[str], float], Tuple[int, str, str]]


@dataclass
class FfmpegSkill:
    root: str
    version: str
    tools: List[str]

    def script(self, tool: str) -> str:
        return os.path.join(self.root, "scripts", f"{tool}.py")


def _candidate(path: str) -> Optional[FfmpegSkill]:
    path = os.path.expanduser(path)
    scripts = os.path.join(path, "scripts")
    if not os.path.isfile(os.path.join(scripts, "scenes.py")):
        return None
    version = "unknown"
    try:
        with open(os.path.join(path, "package.json"), encoding="utf-8") as fh:
            version = str(json.load(fh).get("version", "unknown"))
    except (OSError, ValueError):
        pass
    tools = sorted(f[:-3] for f in os.listdir(scripts) if f.endswith(".py") and not f.startswith("_"))
    return FfmpegSkill(os.path.realpath(path), version, tools)


def candidates(explicit: Optional[str] = None) -> List[str]:
    out = [explicit] if explicit else []
    out += [os.environ[e] for e in ENV_DIRS if os.environ.get(e)]
    out += [os.path.expanduser("~/.claude/skills/ffmpeg-skill"), os.path.join(os.getcwd(), "vendor", "ffmpeg-skill"),
            os.path.join(os.getcwd(), "..", "ffmpeg-skill")]
    return out


def locate(explicit: Optional[str] = None) -> Optional[FfmpegSkill]:
    for c in candidates(explicit):
        found = _candidate(c)
        if found:
            return found
    return None


def subprocess_runner(argv: List[str], timeout: float) -> Tuple[int, str, str]:
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False,
                       stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


class EngineError(Exception):
    def __init__(self, code: str, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


def engine_doctor(skill: FfmpegSkill, *, timeout: float = 60.0, runner: Runner = subprocess_runner) -> Dict[str, Any]:
    """ffmpeg-skill's own doctor (`scripts/_contract.py doctor --json`): ffmpeg / ffprobe versions, overall ok, and
    which of the tools this skill needs are usable. Never raises; an unreadable doctor reads as not ok."""
    argv = [sys.executable, os.path.join(skill.root, "scripts", "_contract.py"), "doctor", "--json"]
    try:
        rc, out, err = runner(argv, timeout)
        doc = json.loads(out)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return {"ok": False, "ffmpeg": None, "ffprobe": None, "tools": {}, "detail": "ffmpeg-skill doctor did not answer"}
    if not isinstance(doc, dict):
        doc = {}
    tools = {t: ((doc.get("tools") or {}).get(t) or {}).get("usable") for t in REQUIRED_TOOLS}
    return {"ok": bool(doc.get("ok")) and all(v == "yes" for v in tools.values()), "ffmpeg": doc.get("ffmpeg"),
            "ffprobe": doc.get("ffprobe"), "tools": tools, "detail": None}


def run_tool(skill: FfmpegSkill, tool: str, args: List[str], *, timeout: float = 3600.0,
             runner: Runner = subprocess_runner) -> Dict[str, Any]:
    """Run one ffmpeg-skill tool with --json and return its document. A non-zero exit or output that is not a
    JSON object is an ENGINE_FAILED error carrying the tool's last stderr line."""
    if tool not in skill.tools:
        raise EngineError("ENGINE_TOOL_MISSING", f"ffmpeg-skill {skill.version} has no '{tool}' tool")
    argv = [sys.executable, skill.script(tool)] + list(args) + ["--json"]
    try:
        rc, out, err = runner(argv, timeout)
    except subprocess.TimeoutExpired:
        raise EngineError("ENGINE_TIMEOUT", f"ffmpeg-skill {tool} did not finish within {timeout:.0f}s")
    last = (err.strip().splitlines() or [""])[-1]
    if rc != 0:
        raise EngineError("ENGINE_FAILED", f"ffmpeg-skill {tool} exited {rc}", last)
    try:
        doc = json.loads(out)
    except ValueError:
        raise EngineError("ENGINE_FAILED", f"ffmpeg-skill {tool} did not print a JSON document", last)
    if not isinstance(doc, dict):
        raise EngineError("ENGINE_FAILED", f"ffmpeg-skill {tool} printed JSON that is not an object", last)
    return doc
