#!/usr/bin/env python3
"""Cloud Render worker.

Runs inside the ``blender-cloud-render`` container on a Vast.ai GPU instance.

    1. download   jobs/<job>/bundle.zip            (packed .blend + externals)
    2. render     the frame range assigned to this worker with Cycles/OptiX
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
            "gpu": gpu_info(),
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
    def __init__(self, status: Status):
        self.q: "queue.Queue[Path | None]" = queue.Queue()
        self.status = status
        self.done = set()
        self.errors = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def enqueue(self, path: Path):
        self.q.put(path)

    def finish(self):
        self.q.put(None)
        self.thread.join()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            try:
                if item in self.done:
                    continue
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
    contiguous = frames == list(range(frames[0], frames[-1] + 1, max(1, FRAME_STEP)))
    if contiguous:
        cmd += ["-s", str(frames[0]), "-e", str(frames[-1]), "-j", str(max(1, FRAME_STEP)), "-a"]
    else:
        cmd += ["-f", ",".join(str(f) for f in frames)]
    return cmd


def snapshot_tree(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def run_render(manifest: dict, frames: list[int], status: Status, uploader: Uploader) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    before_bundle = snapshot_tree(BUNDLE_DIR)
    cmd = blender_cmd(manifest, frames)
    log("running: " + " ".join(cmd))
    env_vars = dict(os.environ)
    env_vars.setdefault("CR_DEVICE", "OPTIX")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, env=env_vars, cwd=str(BUNDLE_DIR),
    )
    status.update(state="rendering")
    seen_saved: set[Path] = set()
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        with _log_lock:
            if _log_fh is not None:
                _log_fh.write(line + "\n")
        m = FRAME_RE.search(line)
        if m:
            status.update(current_frame=int(m.group(1)), progress=m.group(2)[:160])
            continue
        m = SAVED_RE.search(line)
        if m:
            p = Path(m.group(1))
            log(line)
            if p.exists() and p not in seen_saved:
                seen_saved.add(p)
                frame_no = status.data.get("current_frame")
                if frame_no is not None:
                    status.append("frames_done", frame_no)
                uploader.enqueue(p)
            continue
        m = DEVICE_RE.search(line)
        if m:
            status.update(device_used=m.group(1))
        if line.startswith("[gpu_setup]") or "Error" in line or "error" in line[:40] or line.startswith("Blender "):
            log(line)
    rc = proc.wait()
    log(f"blender exited with code {rc}")

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
                raise RuntimeError(f"blender exited with code {rc}")
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
