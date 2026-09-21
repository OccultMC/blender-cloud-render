#!/usr/bin/env python3
"""Cloud Render worker.

Runs inside the ``blender-cloud-render`` container on a Vast.ai GPU instance.

    1. download   jobs/<job>/bundle.zip            (packed .blend + externals)
    2. render     the frame range assigned to this worker with Cycles/OptiX on every GPU
                  (multi-GPU hosts: one Blender per GPU when host RAM allows, see start_runs)
    3. upload     every saved frame to jobs/<job>/frames/<filename> as it lands
    4. report     jobs/<job>/status/worker_<n>.json heartbeat every few seconds
    5. destroy    itself through the Vast API (unless CR_AUTO_DESTROY=0)

All configuration arrives through CR_* environment variables set by the
Blender add-on when it creates the instance.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config

# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


JOB_ID = env("CR_JOB_ID")
WORKER_INDEX = int(env("CR_WORKER_INDEX", "0"))
FRAME_START = int(env("CR_FRAME_START", "1"))
FRAME_END = int(env("CR_FRAME_END", "1"))
FRAME_STEP = int(env("CR_FRAME_STEP", "1"))
FRAME_LIST = env("CR_FRAMES")  # optional explicit "1,2,5" list (used for retries)

R2_ENDPOINT = env("CR_R2_ENDPOINT")
R2_BUCKET = env("CR_R2_BUCKET")
R2_ACCESS_KEY = env("CR_R2_ACCESS_KEY")
R2_SECRET_KEY = env("CR_R2_SECRET_KEY")
R2_PREFIX = env("CR_R2_PREFIX", "blender-cloud-render").strip("/")

AUTO_DESTROY = env("CR_AUTO_DESTROY", "1") == "1"
VAST_API_KEY = env("CR_VAST_API_KEY") or env("VAST_API_KEY")
HEARTBEAT_S = float(env("CR_HEARTBEAT_S", "10"))
UPLOAD_THREADS = int(env("CR_UPLOAD_THREADS", "4"))
# Multi-GPU hosts: AUTO = one Blender per GPU (different frames) when host RAM allows, else COMBINED;
# PER_GPU = always one Blender per GPU; COMBINED = one Blender renders each frame on all GPUs.
GPU_MODE = env("CR_GPU_MODE", "AUTO").upper()

WORK = Path(env("CR_WORK", "/work"))
BUNDLE_DIR = WORK / "bundle"
OUT_DIR = WORK / "out"
LOG_PATH = WORK / f"worker_{WORKER_INDEX}.log"
BLENDER = Path(env("CR_BLENDER", "/opt/blender/current/blender"))
GPU_SETUP = Path(__file__).resolve().parent / "gpu_setup.py"

JOB_PREFIX = f"{R2_PREFIX}/jobs/{JOB_ID}"


def instance_id() -> str:
    """Vast passes its instance id as CONTAINER_ID / VAST_CONTAINERLABEL=C.<id>."""
    iid = env("CR_INSTANCE_ID") or env("CONTAINER_ID")
    if not iid:
        label = env("VAST_CONTAINERLABEL")
        if label.startswith("C."):
            iid = label[2:]
    return iid


INSTANCE_ID = instance_id()

# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #

_log_lock = threading.Lock()
_log_fh = None


def log(msg: str) -> None:
    global _log_fh
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            if _log_fh is None:
                WORK.mkdir(parents=True, exist_ok=True)
                _log_fh = open(LOG_PATH, "a", encoding="utf-8")
            _log_fh.write(line + "\n")
            _log_fh.flush()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# R2
# --------------------------------------------------------------------------- #

def make_s3():
    cfg = Config(
        retries={"max_attempts": 6, "mode": "standard"},
        s3={"addressing_style": "path"},
        max_pool_connections=16,
        # R2 does not accept the newer default CRC checksums on every call.
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
        config=cfg,
    )


S3 = make_s3()


def s3_put_json(key: str, payload: dict) -> None:
    body = json.dumps(payload, indent=1).encode("utf-8")
    S3.put_object(Bucket=R2_BUCKET, Key=key, Body=body, ContentType="application/json")


def s3_get_json(key: str):
    try:
        obj = S3.get_object(Bucket=R2_BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except S3.exceptions.NoSuchKey:
        return None
    except Exception as exc:  # pragma: no cover - network
        log(f"get_json {key} failed: {exc}")
        return None


def s3_upload_file(path: Path, key: str, attempts: int = 5) -> None:
    from boto3.s3.transfer import TransferConfig

    tcfg = TransferConfig(multipart_threshold=64 * 1024 * 1024, multipart_chunksize=32 * 1024 * 1024, max_concurrency=4)
    last = None
    for i in range(attempts):
        try:
            S3.upload_file(str(path), R2_BUCKET, key, Config=tcfg)
            return
        except Exception as exc:  # pragma: no cover - network
            last = exc
            log(f"upload {path.name} attempt {i + 1}/{attempts} failed: {exc}")
            time.sleep(min(30, 2 ** i))
    raise RuntimeError(f"upload failed for {path}: {last}")


def s3_list_keys(prefix: str) -> list[str]:
    keys = []
    token = None
    while True:
        kw = {"Bucket": R2_BUCKET, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = S3.list_objects_v2(**kw)
        keys.extend(o["Key"] for o in resp.get("Contents", []))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return keys


# --------------------------------------------------------------------------- #
# host memory / cgroup diagnostics
# --------------------------------------------------------------------------- #

def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _gb(n) -> str:
    try:
        return f"{float(n) / (1024 ** 3):.1f}G"
    except (TypeError, ValueError):
        return "?"


def cgroup_mem_limit() -> int:
    """Container memory limit in bytes (0 = unlimited / unknown). cgroup v2 then v1."""
    v = _read("/sys/fs/cgroup/memory.max")
    if v and v != "max":
        return int(v)
    v = _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if v.isdigit() and int(v) < (1 << 60):
        return int(v)
    return 0


def cgroup_mem_current() -> int:
    v = _read("/sys/fs/cgroup/memory.current") or _read("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    return int(v) if v.isdigit() else 0


def cgroup_oom_kills() -> int:
    """How many processes the kernel OOM-killed inside this container."""
    for path in ("/sys/fs/cgroup/memory.events", "/sys/fs/cgroup/memory/memory.oom_control"):
        for line in _read(path).splitlines():
            if line.startswith("oom_kill "):
                try:
                    return int(line.split()[1])
                except ValueError:
                    pass
    return 0


def meminfo() -> dict:
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:", "SwapTotal:"):
            out[parts[0][:-1]] = int(parts[1]) * 1024
    return out


def proc_rss(pid: int) -> int:
    for line in _read(f"/proc/{pid}/status").splitlines():
        if line.startswith("VmRSS:"):
            try:
                return int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                return 0
    return 0


def gpu_mem_used() -> tuple[int, int]:
    """(used, total) in bytes for the fullest GPU via nvidia-smi; (0, 0) if unavailable.

    Cycles copies the scene to every card, so on a multi-GPU host the fullest one is the limit.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()
        used, total = max(tuple(int(x.strip()) for x in line.split(",")) for line in out if line.strip())
        return used * 1024 * 1024, total * 1024 * 1024
    except Exception:
        return 0, 0


def gpu_indices() -> list[str]:
    """nvidia-smi indices of the GPUs visible in this container."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=20).stdout
        return [x.strip() for x in out.splitlines() if x.strip().isdigit()]
    except Exception:
        return []


def ram_headroom() -> int:
    """Bytes of host RAM another process could still take (page cache counted as free)."""
    avail = meminfo().get("MemAvailable", 0)
    limit = cgroup_mem_limit()
    if limit:
        cache = 0
        for path in ("/sys/fs/cgroup/memory.stat", "/sys/fs/cgroup/memory/memory.stat"):
            for line in _read(path).splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0] in ("inactive_file", "active_file") and parts[1].isdigit():
                    cache += int(parts[1])
            if cache:
                break
        room = limit - max(0, cgroup_mem_current() - cache)
        avail = min(avail, room) if avail else room
    return max(0, avail)


def host_info() -> dict:
    mi = meminfo()
    limit = cgroup_mem_limit()
    _, vram = gpu_mem_used()
    n_gpus = len(gpu_indices())
    try:
        disk_free = shutil.disk_usage(str(WORK)).free
    except OSError:
        disk_free = 0
    return {
        "ram_total": _gb(mi.get("MemTotal", 0)),
        "ram_avail": _gb(mi.get("MemAvailable", 0)),
        "ram_limit": _gb(limit) if limit else "none",
        "swap": _gb(mi.get("SwapTotal", 0)),
        "vram_total": ((f"{n_gpus}x " if n_gpus > 1 else "") + _gb(vram)) if vram else "?",
        "gpus": n_gpus,
        "disk_free": _gb(disk_free),
        "cpus": os.cpu_count() or 0,
    }


class MemMonitor:
    """Samples Blender RSS (summed over its processes), container usage and VRAM while the render runs."""

    def __init__(self, status: "Status", interval: float = 5.0):
        self.pids: list[int] = []
        self.status = status
        self.interval = interval
        self.limit = cgroup_mem_limit()
        self.total = meminfo().get("MemTotal", 0)
        self.peak_rss = 0
        self.peak_cg = 0
        self.peak_vram = 0
        self.last = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def sample(self) -> str:
        rss = sum(proc_rss(pid) for pid in list(self.pids))
        cg = cgroup_mem_current()
        avail = meminfo().get("MemAvailable", 0)
        vused, vtotal = gpu_mem_used()
        self.peak_rss = max(self.peak_rss, rss)
        self.peak_cg = max(self.peak_cg, cg)
        self.peak_vram = max(self.peak_vram, vused)
        cap = self.limit or self.total
        text = (f"blender RSS {_gb(rss)} (peak {_gb(self.peak_rss)}) | container {_gb(cg)}/{_gb(cap)} "
                f"| host free {_gb(avail)} | VRAM {_gb(vused)}/{_gb(vtotal)} (peak {_gb(self.peak_vram)})")
        self.last = text
        return text

    def _loop(self):
        last_logged = 0.0
        last_logged_rss = 0
        while not self._stop.is_set():
            try:
                text = self.sample()
                self.status.update(mem=text)
                now = time.time()
                grew = abs(self.peak_rss - last_logged_rss) >= 2 * (1024 ** 3)
                if now - last_logged >= 60 or grew:
                    log("[mem] " + text)
                    last_logged = now
                    last_logged_rss = self.peak_rss
            except Exception as exc:
                log(f"[mem] sample failed: {exc}")
            self._stop.wait(self.interval)

    def summary(self) -> str:
        cap = self.limit or self.total
        return (f"peak blender RSS {_gb(self.peak_rss)}, peak container {_gb(self.peak_cg)} of {_gb(cap)}, "
                f"peak VRAM {_gb(self.peak_vram)}")


# --------------------------------------------------------------------------- #
# status / heartbeat
# --------------------------------------------------------------------------- #

class Status:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {
            "job_id": JOB_ID,
            "worker_index": WORKER_INDEX,
            "instance_id": INSTANCE_ID,
            "state": "starting",
            "frame_start": FRAME_START,
            "frame_end": FRAME_END,
            "frame_step": FRAME_STEP,
            "frames_assigned": [],
            "frames_done": [],
            "frames_uploaded": [],
            "current_frame": None,
            "progress": "",
            "device_used": None,
            "render_mode": "",
            "gpu": gpu_info(),
            "host": host_info(),
            "mem": "",
            "blender_mem_peak": "",
            "diagnosis": "",
            "blender_version": None,
            "error": None,
            "started_at": now_iso(),
            "updated_at": now_iso(),
            "finished_at": None,
        }
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def update(self, **kw):
        with self.lock:
            self.data.update(kw)
            self.data["updated_at"] = now_iso()

    def append(self, field: str, value):
        with self.lock:
            lst = self.data.setdefault(field, [])
            if value not in lst:
                lst.append(value)
            self.data["updated_at"] = now_iso()

    def push(self):
        with self.lock:
            snapshot = dict(self.data)
        try:
            s3_put_json(f"{JOB_PREFIX}/status/worker_{WORKER_INDEX}.json", snapshot)
        except Exception as exc:  # pragma: no cover - network
            log(f"status push failed: {exc}")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.push()

    def _loop(self):
        while not self._stop.is_set():
            self.push()
            self._stop.wait(HEARTBEAT_S)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gpu_info() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip() or out.stderr.strip()
    except Exception as exc:
        return f"nvidia-smi unavailable: {exc}"


# --------------------------------------------------------------------------- #
# frame uploader (runs concurrently with the render)
# --------------------------------------------------------------------------- #

class Uploader:
    """Uploads frames while the render runs. Several at once: a slow route to R2 is usually
    slow per connection, and one Blender per GPU saves frames faster than one stream drains them."""

    def __init__(self, status: Status, threads: int = UPLOAD_THREADS):
        self.q: "queue.Queue[Path | None]" = queue.Queue()
        self.status = status
        self.done = set()
        self.claimed = set()
        self.errors = []
        self.threads = [threading.Thread(target=self._run, daemon=True) for _ in range(max(1, threads))]

    def start(self):
        for t in self.threads:
            t.start()

    def enqueue(self, path: Path):
        self.q.put(path)

    def finish(self):
        for _ in self.threads:
            self.q.put(None)
        for t in self.threads:
            t.join()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            try:
                with _log_lock:
                    if item in self.claimed:
                        continue
                    self.claimed.add(item)
                key = self._key_for(item)
                s3_upload_file(item, key)
                self.done.add(item)
                self.status.append("frames_uploaded", item.name)
                log(f"uploaded {item.name} -> {key}")
            except Exception as exc:
                self.errors.append(f"{item.name}: {exc}")
                log(f"UPLOAD ERROR {item.name}: {exc}")

    @staticmethod
    def _key_for(path: Path) -> str:
        if OUT_DIR in path.parents:
            rel = path.relative_to(OUT_DIR).as_posix()
            return f"{JOB_PREFIX}/frames/{rel}"
        rel = path.relative_to(BUNDLE_DIR).as_posix()
        return f"{JOB_PREFIX}/frames/_extra/{rel}"


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #

# Blender 5.x prefixes lines with "HH:MM.mmm  category | ", 4.x does not -> search, don't anchor.
# 4.x: "Fra:5 Mem:1.00M (Peak 1.00M) | Time:00:00.10 | Sample 8/8"
# 5.x: "00:01.532  render | Fra: 5 | Mem: 1M | Rendered 4/16 Tiles, Sample 8/8"
FRAME_RE = re.compile(r"Fra:\s*(\d+)\s*\|?\s*Mem:[^|]*\|\s*(.*)$")
SAVED_RE = re.compile(r"Saved:\s*'(.+?)'")
MEM_RE = re.compile(r"Mem:\s*([\d.]+)\s*([KMG])")
ERROR_HINTS = ("ERROR", "out of memory", "Out of memory", "cannot allocate", "No space left", "Killed", "Segmentation")
LAST_ERROR = {"line": ""}
BLENDER_PEAK = {"mb": 0.0}
DEVICE_RE = re.compile(r"\[gpu_setup\] DEVICE_USED=(\w+)")


def assigned_frames() -> list[int]:
    if FRAME_LIST:
        return sorted({int(x) for x in FRAME_LIST.split(",") if x.strip()})
    return list(range(FRAME_START, FRAME_END + 1, max(1, FRAME_STEP)))


def blender_cmd(manifest: dict, frames: list[int]) -> list[str]:
    blend = BUNDLE_DIR / manifest["blend_file"]
    out_base = manifest.get("output_basename", "")
    out_path = str(OUT_DIR) + "/" + out_base  # keep the artist's file naming pattern
    cmd = [
        str(BLENDER), "-b", str(blend),
        "--python-exit-code", "1",
    ]
    scene = manifest.get("scene")
    if scene:
        cmd += ["-S", scene]
    cmd += ["--python", str(GPU_SETUP), "-o", out_path]
    # any evenly spaced list (a plain range, or every Nth frame for one GPU of N) renders as one animation
    step = frames[1] - frames[0] if len(frames) > 1 else max(1, FRAME_STEP)
    if step > 0 and all(b - a == step for a, b in zip(frames, frames[1:])):
        cmd += ["-s", str(frames[0]), "-e", str(frames[-1]), "-j", str(step), "-a"]
    else:
        cmd += ["-f", ",".join(str(f) for f in frames)]
    return cmd


def snapshot_tree(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()} if root.exists() else set()


SAMPLING_RE = re.compile(r"Sample \d+/\d+")


class RenderRun:
    """One Blender process, optionally pinned to some of the GPUs, with its output pumped on a thread."""

    def __init__(self, manifest: dict, frames: list[int], gpus, status: Status, uploader: Uploader,
                 seen_saved: set, tag: str = ""):
        self.frames = frames
        self.gpus = gpus
        self.prefix = f"[{tag}] " if tag else ""
        self.status = status
        self.uploader = uploader
        self.seen_saved = seen_saved
        self.frames_done: list[int] = []
        self.cur_frame = None
        self.rc = None
        self.loaded = threading.Event()  # first frame is sampling (scene fully in host RAM), or the process ended
        cmd = blender_cmd(manifest, frames)
        env_vars = dict(os.environ)
        env_vars.setdefault("CR_DEVICE", "OPTIX")
        if gpus:
            env_vars["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"  # same numbering as nvidia-smi
            env_vars["CUDA_VISIBLE_DEVICES"] = ",".join(gpus)
        log(f"{self.prefix}running: " + " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, env=env_vars, cwd=str(BUNDLE_DIR),
        )
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def join(self) -> int:
        self.thread.join()
        return self.rc

    def kill(self) -> None:
        self.proc.kill()
        self.thread.join(timeout=60)

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        try:
            for raw in self.proc.stdout:
                self._line(self.prefix + raw.rstrip("\n"))
        finally:
            self.rc = self.proc.wait()
            self.loaded.set()

    def _line(self, line: str) -> None:
        status = self.status
        with _log_lock:
            if _log_fh is not None:
                _log_fh.write(line + "\n")
        if any(h in line for h in ERROR_HINTS) and "Loading render kernels" not in line:
            LAST_ERROR["line"] = line.strip()[:200]
            print(line, flush=True)
        m = FRAME_RE.search(line)
        if m:
            self.cur_frame = int(m.group(1))
            status.update(current_frame=self.cur_frame, progress=(self.prefix + m.group(2))[:160])
            if SAMPLING_RE.search(m.group(2)):
                self.loaded.set()
            mm = MEM_RE.search(line)
            if mm:
                mb = float(mm.group(1)) * {"K": 1 / 1024.0, "M": 1.0, "G": 1024.0}[mm.group(2)]
                if mb > BLENDER_PEAK["mb"]:
                    BLENDER_PEAK["mb"] = mb
                    status.update(blender_mem_peak=f"{mb / 1024.0:.1f}G")
            return
        m = SAVED_RE.search(line)
        if m:
            p = Path(m.group(1))
            print(line, flush=True)  # already in the log file via the raw write above
            self.loaded.set()
            if p.exists() and p not in self.seen_saved:
                self.seen_saved.add(p)
                if self.cur_frame is not None:
                    if self.cur_frame not in self.frames_done:
                        self.frames_done.append(self.cur_frame)
                    status.append("frames_done", self.cur_frame)
                self.uploader.enqueue(p)
            return
        m = DEVICE_RE.search(line)
        if m:
            status.update(device_used=m.group(1))
        body = line[len(self.prefix):]
        if body.startswith("[gpu_setup]") or "Error" in body or "error" in body[:40] or body.startswith("Blender "):
            print(line, flush=True)


def gpu_groups(gpus: list[str], n: int) -> list[list[str]]:
    """Split the GPUs into n groups of (nearly) equal size."""
    n = max(1, min(n, len(gpus)))
    base, extra = divmod(len(gpus), n)
    groups, i = [], 0
    for g in range(n):
        size = base + (1 if g < extra else 0)
        groups.append(gpus[i:i + size])
        i += size
    return groups


def deal_frames(frames: list[int], groups: list[list[str]]) -> list[list[int]]:
    """Interleave frames over the groups in proportion to their GPU count.

    Neighbouring frames cost about the same, so every group finishes at about the same time.
    """
    slots = [gi for r in range(max(len(g) for g in groups)) for gi, g in enumerate(groups) if r < len(g)]
    shares: list[list[int]] = [[] for _ in groups]
    for k, f in enumerate(frames):
        shares[slots[k % len(slots)]].append(f)
    return shares


def start_runs(frames: list[int], gpus: list[str], monitor: MemMonitor, status: Status, make_run) -> list[RenderRun]:
    """Start the Blender process(es) for this host.

    One GPU, one frame, or COMBINED: a single Blender renders every frame on all GPUs.
    Otherwise one Blender per GPU, each on its own frames - Cycles scales better that way
    because the per-frame CPU work (scene sync, BVH build) runs in parallel too. Every process
    holds its own copy of the scene in host RAM, so AUTO loads one first, measures it, and
    only starts as many as fit (grouping the GPUs when not all of them do).
    """
    n = min(len(gpus), len(frames))
    if GPU_MODE == "COMBINED" or n < 2:
        if len(gpus) > 1:
            log(f"{len(gpus)} GPUs: one Blender process renders each frame on all of them")
            status.update(render_mode=f"1 process x {len(gpus)} GPUs")
        return [make_run(frames, None, "")]

    def launch(todo: list[int], count: int, skip_first: bool = False) -> list[RenderRun]:
        groups = gpu_groups(gpus, count)
        shares = deal_frames(todo, groups)
        sizes = "/".join(str(len(g)) for g in groups)
        if not skip_first:
            log(f"{len(gpus)} GPUs: {len(groups)} Blender processes on {sizes} GPU(s), each rendering its own frames")
            status.update(render_mode=f"{len(groups)} processes x {sizes} GPUs")
        return [make_run(s, g, "gpu" + ",".join(g)) for s, g in list(zip(shares, groups))[1 if skip_first else 0:] if s]

    if GPU_MODE != "AUTO":
        return launch(frames, n)
    log(f"{len(gpus)} GPUs: loading the scene in one process first to see how many fit in host RAM")
    groups = gpu_groups(gpus, n)
    first = make_run(deal_frames(frames, groups)[0], groups[0], "gpu" + ",".join(groups[0]))
    while not first.loaded.wait(5):
        pass
    if first.rc not in (None, 0):
        return [first]  # could not even render alone: the caller retries on all GPUs in one process
    monitor.sample()
    rss, room = monitor.peak_rss, ram_headroom()
    fit = n if not rss else 1 + int(room // (rss * 1.25))
    log(f"[mem] one process peaks at {_gb(rss)} RSS, RAM headroom {_gb(room)}: room for {min(fit, n)} of {n} processes")
    if fit >= n or first.rc is not None:
        status.update(render_mode=f"{n} processes x {'/'.join(str(len(g)) for g in groups)} GPUs")
        return [first] + launch(frames, n, skip_first=True)
    # not enough RAM for one process per GPU: restart with fewer, larger GPU groups
    first.kill()
    if first.proc.pid in monitor.pids:
        monitor.pids.remove(first.proc.pid)
    left = [f for f in frames if f not in first.frames_done]
    if not left:
        return []
    if fit < 2 or len(left) < 2:
        log(f"host RAM only fits one copy of the scene: one Blender process renders each frame on all {len(gpus)} GPUs")
        status.update(render_mode=f"1 process x {len(gpus)} GPUs (RAM-limited)")
        return [make_run(left, None, "")]
    return launch(left, min(fit, len(left)))


def run_render(manifest: dict, frames: list[int], status: Status, uploader: Uploader) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    before_bundle = snapshot_tree(BUNDLE_DIR)
    status.update(state="rendering")
    monitor = MemMonitor(status)
    monitor.start()
    seen_saved: set[Path] = set()
    all_runs: list[RenderRun] = []

    def make_run(todo: list[int], gpus, tag: str) -> RenderRun:
        run = RenderRun(manifest, todo, gpus, status, uploader, seen_saved, tag)
        all_runs.append(run)
        monitor.pids.append(run.proc.pid)
        return run

    runs = start_runs(frames, gpu_indices(), monitor, status, make_run)
    for run in runs:
        run.join()
    failed = [r for r in runs if r.rc != 0]
    rc = failed[0].rc if failed else 0
    if failed and any(r.gpus for r in runs):
        # a per-GPU process died (usually host RAM): finish its frames with one process on all GPUs
        done = {f for r in all_runs for f in r.frames_done}
        missing = [f for f in frames if f not in done]
        if missing:
            log(f"{len(failed)} per-GPU process(es) failed (exit {rc}); rendering the {len(missing)} missing "
                f"frame(s) with one process on all GPUs")
            status.update(render_mode="1 process x all GPUs (fallback)")
            rc = make_run(missing, None, "").join()
        else:
            log(f"a Blender process exited with code {rc} after saving all of its frames - ignoring")
            rc = 0
    monitor.stop()
    try:
        monitor.sample()
    except Exception:
        pass
    log(f"blender exited with code {rc}")
    log(f"[mem] {monitor.summary()}; blender reported peak {BLENDER_PEAK['mb'] / 1024.0:.1f}G")
    reason = ""
    if rc < 0:
        kills = cgroup_oom_kills()
        sig = -rc
        if kills or sig == 9:
            limit = monitor.limit or monitor.total
            reason = (f"blender was killed by signal {sig}"
                      + (f" - cgroup reports {kills} OOM kill(s)" if kills else "")
                      + f": host RAM exhausted (container limit {_gb(limit)}, peak blender RSS {_gb(monitor.peak_rss)}). "
                        "Pick a machine with more RAM or reduce the scene (dicing rate / texture sizes / adaptive subdivision).")
        else:
            reason = f"blender was killed by signal {sig}"
        try:
            dmesg = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=10).stdout.splitlines()
            for line in [x for x in dmesg if "Out of memory" in x or "oom-kill" in x or "Killed process" in x][-3:]:
                log("[dmesg] " + line[:200])
        except Exception:
            pass
    elif rc != 0 and LAST_ERROR["line"]:
        reason = f"blender exited with code {rc}: {LAST_ERROR['line']}"
        if BLENDER_PEAK["mb"]:
            reason += f" (peak {BLENDER_PEAK['mb'] / 1024.0:.1f}G)"
    if reason:
        log("DIAGNOSIS: " + reason)
        status.update(diagnosis=reason)

    # Anything written that we did not catch via 'Saved:' (multilayer passes,
    # File Output nodes writing next to the .blend, ...).
    for p in sorted(snapshot_tree(OUT_DIR)):
        if p not in seen_saved:
            uploader.enqueue(p)
    for p in sorted(snapshot_tree(BUNDLE_DIR) - before_bundle):
        if p.suffix.lower() in {".blend1", ".log"}:
            continue
        uploader.enqueue(p)
    return rc


# --------------------------------------------------------------------------- #
# self destruct
# --------------------------------------------------------------------------- #

def vast_request(method: str, path: str):
    req = urllib.request.Request(
        f"https://console.vast.ai/api/v0{path}",
        headers={"Authorization": f"Bearer {VAST_API_KEY}", "Accept": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def self_destruct() -> None:
    iid = INSTANCE_ID
    if not iid:
        mapping = s3_get_json(f"{JOB_PREFIX}/workers.json") or {}
        iid = str(mapping.get(str(WORKER_INDEX), {}).get("instance_id", ""))
    if not iid or not VAST_API_KEY:
        log("self-destruct skipped: instance id or Vast key unavailable (add-on will clean up)")
        return
    for attempt in range(8):
        try:
            code, _ = vast_request("DELETE", f"/instances/{iid}/")
            log(f"DELETE instance {iid} -> {code}")
        except Exception as exc:
            log(f"DELETE instance {iid} failed: {exc}")
            code = 0
        time.sleep(8)
        try:
            _, body = vast_request("GET", "/instances/")
            live = {str(i.get("id")) for i in json.loads(body).get("instances", [])}
            if iid not in live:
                log("instance confirmed destroyed")
                return
        except Exception as exc:
            log(f"verify listing failed: {exc}")
            if code in (200, 404):
                return
        time.sleep(5 * (attempt + 1))
    log("WARNING: could not confirm self-destruct; add-on cleanup will handle it")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    status = Status()
    status.start()
    rc = 1
    uploader = Uploader(status)
    try:
        if not JOB_ID or not R2_ENDPOINT or not R2_BUCKET:
            raise RuntimeError("missing CR_JOB_ID / CR_R2_ENDPOINT / CR_R2_BUCKET")
        frames = assigned_frames()
        status.update(frames_assigned=frames)
        log(f"job {JOB_ID} worker {WORKER_INDEX} instance {INSTANCE_ID or '?'} frames {frames[0]}..{frames[-1]} ({len(frames)})")

        # Blender version actually present
        try:
            ver = subprocess.run([str(BLENDER), "--version"], capture_output=True, text=True, timeout=60).stdout.splitlines()[0]
        except Exception as exc:
            ver = f"unknown ({exc})"
        status.update(blender_version=ver)
        log(f"blender: {ver}")
        hi = status.data.get("host") or {}
        log(f"host: RAM {hi.get('ram_total')} total / {hi.get('ram_avail')} available, container limit {hi.get('ram_limit')}, "
            f"swap {hi.get('swap')}, VRAM {hi.get('vram_total')}, disk free {hi.get('disk_free')}, cpus {hi.get('cpus')}")

        # 1. bundle
        status.update(state="downloading")
        BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
        zip_path = WORK / "bundle.zip"
        key = f"{JOB_PREFIX}/bundle.zip"
        log(f"downloading s3://{R2_BUCKET}/{key}")
        S3.download_file(R2_BUCKET, key, str(zip_path))
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(BUNDLE_DIR)
        zip_path.unlink(missing_ok=True)
        manifest = json.loads((BUNDLE_DIR / "manifest.json").read_text(encoding="utf-8"))
        log(f"bundle: {manifest.get('blend_file')} scene={manifest.get('scene')} fmt={manifest.get('file_format')} denoise={manifest.get('use_denoising')}")

        # Skip frames that already exist (retried chunk after a dead worker).
        existing = {Path(k).name for k in s3_list_keys(f"{JOB_PREFIX}/frames/")}
        if existing and manifest.get("frame_filenames"):
            todo = [f for f in frames if manifest["frame_filenames"].get(str(f)) not in existing]
            if todo != frames:
                log(f"skipping {len(frames) - len(todo)} frames already in R2")
                frames = todo
        if not frames:
            log("nothing left to render")
            status.update(state="done", finished_at=now_iso())
            rc = 0
        else:
            # 2. render + 3. upload
            uploader.start()
            rc = run_render(manifest, frames, status, uploader)
            status.update(state="uploading")
            uploader.finish()
            if uploader.errors:
                raise RuntimeError("frame upload errors: " + "; ".join(uploader.errors[:5]))
            if rc != 0:
                raise RuntimeError(status.data.get("diagnosis") or f"blender exited with code {rc}")
            status.update(state="done", finished_at=now_iso())
            log("worker finished OK")
    except Exception as exc:
        rc = 1
        log("FATAL: " + "".join(traceback.format_exception(exc)).strip())
        status.update(state="failed", error=str(exc)[:2000], finished_at=now_iso())
    finally:
        try:
            if _log_fh is not None:
                _log_fh.flush()
            if LOG_PATH.exists():
                s3_upload_file(LOG_PATH, f"{JOB_PREFIX}/logs/worker_{WORKER_INDEX}.log", attempts=3)
        except Exception as exc:
            log(f"log upload failed: {exc}")
        status.stop()
        if AUTO_DESTROY:
            self_destruct()
    return rc


if __name__ == "__main__":
    sys.exit(main())
