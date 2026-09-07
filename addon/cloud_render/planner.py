"""Split an animation's frame list into contiguous whole-frame chunks, one per worker."""
from __future__ import annotations

import math


def frame_list(frame_start: int, frame_end: int, frame_step: int = 1) -> list[int]:
    step = max(1, int(frame_step))
    if frame_end < frame_start:
        return []
    return list(range(int(frame_start), int(frame_end) + 1, step))


def split_frames(frame_start: int, frame_end: int, frame_step: int, workers: int) -> list[dict]:
    """Return one dict per worker: {index, frame_start, frame_end, frame_step, frames}.

    Frames are divided as evenly as possible with ceil(total / workers) frames per
    worker so nobody gets a fractional frame.  If there are more workers than
    frames, the worker count is reduced to the frame count.
    """
    frames = frame_list(frame_start, frame_end, frame_step)
    if not frames:
        return []
    n = max(1, min(int(workers), len(frames)))
    chunk = math.ceil(len(frames) / n)
    plan = []
    for i in range(n):
        part = frames[i * chunk:(i + 1) * chunk]
        if not part:
            break
        plan.append({
            "index": i,
            "frame_start": part[0],
            "frame_end": part[-1],
            "frame_step": max(1, int(frame_step)),
            "frames": part,
        })
    return plan


def describe_plan(plan: list[dict]) -> str:
    if not plan:
        return "no frames"
    total = sum(len(p["frames"]) for p in plan)
    sizes = sorted({len(p["frames"]) for p in plan})
    per = f"{sizes[0]}" if len(sizes) == 1 else f"{sizes[0]}-{sizes[-1]}"
    return f"{total} frames over {len(plan)} workers ({per} frames each)"
