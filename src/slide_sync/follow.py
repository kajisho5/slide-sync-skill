"""FOLLOW: slide changes from a recording that is still being written (`detect --follow`), as JSON lines.

The recording grows on disk while the talk goes on. Every `poll` seconds, when the file has grown, ffmpeg-skill `scenes`
reads it as it stands (the part written so far) with the same settings as a batch `detect`. A change is reported once it
lies at least `guard` seconds before the end of what could be read: `scenes` decides a cut from the frames around it
(+-12 frames) and the cuts kept before it (`--min-scene`), and drops a last cut closer than `--min-scene` to the end, so a
change that far from the readable end is the change the finished file will have (docs/decisions.md ADR-007). When the
file has not changed for `settle` seconds the recording is taken as closed, and one batch `detect` of the whole file
gives the timeline; changes it has that were not reported yet are reported then, and a reported change it does not have
is retracted (never seen on the fixtures, but said rather than hidden).

Events, one JSON object per line: `start`, `pass` (what was readable), `change` (a slide change, final), `retract`,
`waiting` (the file is not readable yet), `done` (the timeline, as `detect` gives it). Whether the recorder still holds
the file open is not detected (no portable way): the settle window is the end-of-recording signal, as in ingest-skill's
`offload --follow`.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional

from .core import DEFAULT_MIN_DWELL, DEFAULT_THRESHOLD, SlideSyncError, _round, detect
from .engine import EngineError, FfmpegSkill, Runner, run_tool, subprocess_runner

DEFAULT_SETTLE = 30.0      # seconds unchanged before the recording counts as closed (ingest-skill's default)
DEFAULT_POLL = 5.0         # seconds between looks at the file; a pass reads the whole file written so far
GUARD_EXTRA = 1.0          # seconds beyond --min-dwell kept clear of the readable end (scenes' +-12 frame window)

Emit = Callable[[Dict[str, Any]], None]


def guard_for(min_dwell: float) -> float:
    """How far before the readable end a change must lie to be final: scenes drops a last cut closer than min_dwell to
    the end, and decides a cut from +-12 frames around it (half a second at 25 fps), so min_dwell + 1 s."""
    return min_dwell + GUARD_EXTRA


def confirmed(scene_doc: Dict[str, Any], guard: float) -> List[float]:
    """The change times of one pass over the partial file that will not change: those at least `guard` before the
    readable end (`duration` of the pass)."""
    duration = scene_doc.get("duration")
    scenes = scene_doc.get("scenes")
    if not isinstance(duration, (int, float)) or duration <= 0 or not isinstance(scenes, list) or not scenes:
        return []
    starts = sorted(_round(s["start"]) for s in scenes)[1:]
    return [t for t in starts if t <= float(duration) - guard]


def _stat(path: str) -> Optional[tuple]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def follow(skill: FfmpegSkill, video: str, emit: Emit, *, threshold: float = DEFAULT_THRESHOLD,
           min_dwell: float = DEFAULT_MIN_DWELL, settle: float = DEFAULT_SETTLE, poll: float = DEFAULT_POLL,
           follow_for: Optional[float] = None, timeout: float = 3600.0, runner: Runner = subprocess_runner,
           clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """Follow a growing recording until it has been unchanged for `settle` seconds (or `follow_for` seconds have passed),
    emitting events as it goes. Returns the final timeline (`detect` of the file as it is at the end), with `follow`
    (passes, changes reported while recording, retractions, whether it closed)."""
    if not (0 < threshold <= 100):
        raise SlideSyncError("INVALID_ARGUMENT", "--threshold must be in (0, 100]")
    if min_dwell <= 0:
        raise SlideSyncError("INVALID_ARGUMENT", "--min-dwell must be above 0")
    if settle <= 0 or poll <= 0:
        raise SlideSyncError("INVALID_ARGUMENT", "--settle and --poll must be above 0")
    if follow_for is not None and follow_for <= 0:
        raise SlideSyncError("INVALID_ARGUMENT", "--follow-for must be above 0")
    guard = guard_for(min_dwell)
    started = clock()
    emit({"event": "start", "video": os.path.abspath(video), "threshold": threshold, "min_dwell": min_dwell,
          "settle": settle, "poll": poll, "follow_for": follow_for, "guard": guard})
    reported: List[float] = []
    passes = 0
    last_stat: Optional[tuple] = None
    last_analysed: Optional[tuple] = None
    unchanged_since: Optional[float] = None
    waiting_said = False
    closed = False
    while True:
        now = clock()
        st = _stat(video)
        if st != last_stat:
            last_stat, unchanged_since = st, now
        elif st is not None and unchanged_since is not None and now - unchanged_since >= settle:
            closed = True
            break
        if follow_for is not None and now - started >= follow_for:
            break
        if st is None and passes == 0 and now - started >= settle:
            raise SlideSyncError("INPUT_NOT_FOUND", f"{video} did not appear within --settle {settle:g}s")
        if st is not None and st != last_analysed and st[0] > 0:
            last_analysed = st
            try:
                doc = run_tool(skill, "scenes", [video, "--threshold", str(threshold), "--min-scene", str(min_dwell)],
                               timeout=timeout, runner=runner)
            except EngineError as e:
                if e.code == "ENGINE_TOOL_MISSING":
                    raise
                doc = None
                if not waiting_said:
                    emit({"event": "waiting", "reason": "the file cannot be read yet", "detail": e.detail or str(e)})
                    waiting_said = True
            if doc is not None:
                passes += 1
                readable = doc.get("duration") if isinstance(doc.get("duration"), (int, float)) else None
                found = confirmed(doc, guard)
                emit({"event": "pass", "pass": passes, "size": st[0], "readable": _round(readable) if readable else None,
                      "confirmed_until": _round(readable - guard) if readable and readable > guard else 0.0})
                if not readable and not waiting_said:
                    emit({"event": "waiting", "reason": "ffmpeg-skill cannot measure the length of the file while it "
                                                        "grows (an MKV or MP4 written live): changes come when it closes"})
                    waiting_said = True
                for t in found:
                    if t > (reported[-1] if reported else 0.0) + 1e-6:
                        reported.append(t)
                        emit({"event": "change", "slide": len(reported) + 1, "time": t, "pass": passes,
                              "while_recording": True})
        sleep(poll)

    if not os.path.isfile(video):
        raise SlideSyncError("INPUT_NOT_FOUND", f"no such file: {video}")
    timeline = detect(skill, video, threshold=threshold, min_dwell=min_dwell, timeout=timeout, runner=runner)
    final = timeline["changes"]
    retracted = [t for t in reported if not any(abs(t - f) < 1e-3 for f in final)]
    for t in retracted:
        emit({"event": "retract", "time": t, "reason": "not a change in the whole file"})
    during = [t for t in reported if t not in retracted]
    late = 0
    for i, t in enumerate(final):
        if not any(abs(t - r) < 1e-3 for r in during):
            late += 1
            emit({"event": "change", "slide": i + 2, "time": t, "pass": None, "while_recording": False})
    timeline["follow"] = {"passes": passes, "closed": closed, "settle": settle, "poll": poll, "guard": guard,
                          "reported_while_recording": len(during), "reported_at_close": late,
                          "retracted": retracted}
    if not closed:
        timeline.setdefault("warnings", []).append(
            f"--follow-for {follow_for:g}s ran out before the file settled: the recording may still be going, so the "
            "last slide's end is where the file ended at that moment")
    emit({"event": "done", "closed": closed, "timeline": timeline})
    return timeline
