"""Module-level runtime state shared between the job thread, operators and the panel.

Only the main thread touches bpy; the job thread only mutates the CloudJob, and
a bpy.app.timers callback copies a snapshot out for drawing.
"""
from __future__ import annotations

from typing import Optional

import bpy

from .job import CloudJob

ACTIVE_JOB: Optional[CloudJob] = None
SNAPSHOT: dict = {}
OFFERS_PREVIEW: list = []
OFFERS_PREVIEW_MSG: str = ""
_timer_running = False


def set_job(job: Optional[CloudJob]) -> None:
    global ACTIVE_JOB, SNAPSHOT
    ACTIVE_JOB = job
    SNAPSHOT = job.snapshot() if job else {}
    if job is not None:
        start_ui_timer()


def redraw_properties() -> None:
    for wm in bpy.data.window_managers:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type in {"PROPERTIES", "TOPBAR"}:
                    area.tag_redraw()


def _tick():
    global SNAPSHOT, _timer_running
    job = ACTIVE_JOB
    if job is None:
        _timer_running = False
        return None
    SNAPSHOT = job.snapshot()
    redraw_properties()
    if not job.is_running and not job.thread.is_alive():
        _timer_running = False
        return None
    return 1.0


def start_ui_timer() -> None:
    global _timer_running
    if _timer_running:
        return
    _timer_running = True
    bpy.app.timers.register(_tick, first_interval=0.5, persistent=True)


def stop_ui_timer() -> None:
    global _timer_running
    _timer_running = False
    try:
        if bpy.app.timers.is_registered(_tick):
            bpy.app.timers.unregister(_tick)
    except Exception:
        pass
