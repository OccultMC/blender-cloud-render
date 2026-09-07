"""Cloud render job: runs in a background thread, never touches bpy.

Lifecycle:  packing -> uploading -> provisioning -> rendering -> finishing -> done
            (or failed / cancelled).  A UI timer on the main thread reads
            ``CloudJob.snapshot()`` to draw progress in the render panel.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

from . import packer, planner
from .r2 import R2Client, R2Error
from .vast import VastClient, VastError, ghcr_login_string, min_driver_for, resolve_image_digest

RUNNING_STATES = {"packing", "uploading", "provisioning", "rendering", "finishing", "resuming"}
DEAD_INSTANCE_STATES = {"exited", "offline", "unknown", "error", "errored", "crashed", "lost", "stopped"}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobConfig:
    job_id: str
    blend_path: str
    blend_name: str            # basename without .blend
    blend_dir: str
    job_dir: str               # local folder for bundle/job.json
    output_dir: str            # where frames land locally
    tmp_blend: str
    blender_exe: str
    blender_version: tuple
    blender_version_string: str
    plan: list                 # planner.split_frames output
    frame_step: int
    # cloud
    image: str
    ghcr_user: str
    ghcr_token: str
    pin_digest: bool
    vast_key: str
    r2_account: str
    r2_access_key: str
    r2_secret_key: str
    r2_bucket: str
    r2_endpoint: str
    r2_prefix: str
    # offer filters
    series: list
    min_vram_gb: int
    disk_gb: int
    max_dph: float
    min_reliability: float
    min_inet_down: int
    # behaviour
    auto_download: bool = True
    auto_destroy: bool = True
    max_retries: int = 2
    poll_interval: float = 10.0
    stale_minutes: float = 20.0
    loading_timeout_minutes: float = 20.0
    keep_bundle_in_r2: bool = False
    device: str = "OPTIX"


@dataclass
class WorkerState:
    index: int
    frames: list
    frame_start: int
    frame_end: int
    frame_step: int
    instance_id: Optional[int] = None
    offer: dict = field(default_factory=dict)
    state: str = "pending"          # pending/creating/loading/starting/downloading/rendering/uploading/done/failed/dead
    attempts: int = 0
    frames_done: list = field(default_factory=list)
    progress: str = ""
    current_frame: Optional[int] = None
    device_used: Optional[str] = None
    gpu: str = ""
    error: str = ""
    created_at: str = ""
    last_status_at: str = ""
    failed_machines: list = field(default_factory=list)

    @property
    def remaining(self) -> list:
        done = set(self.frames_done)
        return [f for f in self.frames if f not in done]


class CloudJob:
    def __init__(self, cfg: JobConfig, resume: bool = False):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.workers: list[WorkerState] = [
            WorkerState(index=p["index"], frames=list(p["frames"]), frame_start=p["frame_start"],
                        frame_end=p["frame_end"], frame_step=p["frame_step"]) for p in cfg.plan
        ]
        self.status = "resuming" if resume else "packing"
        self.message = ""
        self.error = ""
        self.log_lines: list[str] = []
        self.frames_total = sum(len(w.frames) for w in self.workers)
        self.frames_in_r2: set = set()
        self.downloaded: set = set()
        self.frame_filenames: dict = {}
        self.manifest: dict = {}
        self.pack_report: dict = {}
        self.bundle_bytes = 0
        self.upload_progress = 0.0
        self.cost_per_hour = 0.0
        self.started_at = _now()
        self.render_started_at = ""
        self.finished_at = ""
        self.instances_ever: set = set()
        self.resume = resume
        self.thread = threading.Thread(target=self._run, name=f"cloudrender-{cfg.job_id}", daemon=True)
        self.r2 = R2Client(cfg.r2_account, cfg.r2_access_key, cfg.r2_secret_key, cfg.r2_bucket, cfg.r2_endpoint)
        self.vast = VastClient(cfg.vast_key)

    # ------------------------------------------------------------------ misc
    @property
    def prefix(self) -> str:
        return f"{self.cfg.r2_prefix.strip('/')}/jobs/{self.cfg.job_id}"

    def log(self, msg: str) -> None:
        line = f"{_dt.datetime.now().strftime('%H:%M:%S')} {msg}"
        with self.lock:
            self.log_lines.append(line)
            if len(self.log_lines) > 400:
                del self.log_lines[:100]
        print("[cloud_render] " + msg, flush=True)

    def set_status(self, status: str, message: str = "") -> None:
        with self.lock:
            self.status = status
            if message:
                self.message = message
        self.log(f"{status}: {message}" if message else status)
        self.save()

    @property
    def is_running(self) -> bool:
        return self.status in RUNNING_STATES

    def frames_done_count(self) -> int:
        return len(self.frames_in_r2)

    def snapshot(self) -> dict:
        with self.lock:
            done = len(self.frames_in_r2)
            return {
                "job_id": self.cfg.job_id,
                "status": self.status,
                "message": self.message,
                "error": self.error,
                "frames_total": self.frames_total,
                "frames_done": done,
                "frames_downloaded": len(self.downloaded),
                "progress": (done / self.frames_total) if self.frames_total else 0.0,
                "upload_progress": self.upload_progress,
                "cost_per_hour": self.cost_per_hour,
                "cost_so_far": self.cost_so_far(),
                "workers": [asdict(w) for w in self.workers],
                "log_tail": list(self.log_lines[-12:]),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "output_dir": self.cfg.output_dir,
                "warnings": list(self.pack_report.get("warnings", [])) + [
                    f"missing file: {m}" for m in self.pack_report.get("missing", [])
                ],
            }

    def cost_so_far(self) -> float:
        if not self.render_started_at:
            return 0.0
        start = _dt.datetime.fromisoformat(self.render_started_at)
        end = _dt.datetime.fromisoformat(self.finished_at) if self.finished_at else _dt.datetime.now(_dt.timezone.utc)
        hours = max(0.0, (end - start).total_seconds() / 3600.0)
        return hours * self.cost_per_hour

    # ------------------------------------------------------------- persist
    def save(self) -> None:
        try:
            os.makedirs(self.cfg.job_dir, exist_ok=True)
            with self.lock:
                cfg = asdict(self.cfg)
                for secret in ("vast_key", "r2_access_key", "r2_secret_key", "ghcr_token"):
                    cfg[secret] = ""
                data = {
                    "config": cfg,
                    "status": self.status,
                    "message": self.message,
                    "error": self.error,
                    "workers": [asdict(w) for w in self.workers],
                    "frame_filenames": self.frame_filenames,
                    "frames_in_r2": sorted(self.frames_in_r2),
                    "downloaded": sorted(self.downloaded),
                    "cost_per_hour": self.cost_per_hour,
                    "started_at": self.started_at,
                    "render_started_at": self.render_started_at,
                    "finished_at": self.finished_at,
                    "instances_ever": sorted(self.instances_ever),
                    "manifest": self.manifest,
                    "pack_report": self.pack_report,
                    "log_tail": self.log_lines[-100:],
                }
            tmp = os.path.join(self.cfg.job_dir, "job.json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, os.path.join(self.cfg.job_dir, "job.json"))
        except Exception as exc:
            print(f"[cloud_render] could not save job.json: {exc}")

    @classmethod
    def load(cls, job_dir: str, secrets: dict) -> "CloudJob":
        with open(os.path.join(job_dir, "job.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        cfg_d = data["config"]
        cfg_d.update({k: v for k, v in secrets.items() if k in cfg_d})
        cfg_d["blender_version"] = tuple(cfg_d["blender_version"])
        cfg = JobConfig(**cfg_d)
        job = cls(cfg, resume=True)
        job.workers = [WorkerState(**w) for w in data["workers"]]
        job.frame_filenames = data.get("frame_filenames", {})
        job.frames_in_r2 = set(data.get("frames_in_r2", []))
        job.downloaded = set(data.get("downloaded", []))
        job.cost_per_hour = data.get("cost_per_hour", 0.0)
        job.started_at = data.get("started_at", job.started_at)
        job.render_started_at = data.get("render_started_at", "")
        job.finished_at = data.get("finished_at", "")
        job.instances_ever = set(data.get("instances_ever", []))
        job.manifest = data.get("manifest", {})
        job.pack_report = data.get("pack_report", {})
        job.log_lines = list(data.get("log_tail", []))
        job.status = data.get("status", "rendering")
        job.message = data.get("message", "")
        job.error = data.get("error", "")
        return job

    # ---------------------------------------------------------------- run
    def start(self) -> None:
        self.thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.log("cancel requested")

    def _run(self) -> None:
        try:
            if self.resume:
                if self.status in ("done", "failed", "cancelled"):
                    return
                self.set_status("rendering", "resumed monitoring")
                self._monitor()
            else:
                self._stage_pack()
                self._check_cancel()
                self._stage_upload()
                self._check_cancel()
                self._stage_provision()
                self._check_cancel()
                self._monitor()
        except _Cancelled:
            self._teardown("cancelled", "cancelled by user")
        except Exception as exc:
            self.error = str(exc)
            self.log("ERROR: " + "".join(traceback.format_exception(exc)).strip()[-1500:])
            self._teardown("failed", str(exc)[:300])
        finally:
            self._cleanup_local()
            self.save()

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise _Cancelled()

    # --------------------------------------------------------------- stages
    def _stage_pack(self) -> None:
        cfg = self.cfg
        self.set_status("packing", "packing textures, libraries and caches into the bundle")
        bundle_dir = os.path.join(cfg.job_dir, "bundle")
        if os.path.isdir(bundle_dir):
            shutil.rmtree(bundle_dir, ignore_errors=True)
        manifest, report = packer.run_pack(cfg.blender_exe, cfg.tmp_blend, bundle_dir, cfg.blend_name,
                                           cfg.blend_dir, self.log)
        with self.lock:
            self.manifest = manifest
            self.pack_report = report
            self.frame_filenames = manifest.get("frame_filenames", {})
        for w in report.get("warnings", []):
            self.log("pack warning: " + w)
        for m in report.get("missing", []):
            self.log("MISSING FILE: " + m)
        zip_path = os.path.join(cfg.job_dir, "bundle.zip")
        self.bundle_bytes = packer.zip_bundle(bundle_dir, zip_path, self.log)
        shutil.rmtree(bundle_dir, ignore_errors=True)
        self.save()

    def _stage_upload(self) -> None:
        cfg = self.cfg
        zip_path = os.path.join(cfg.job_dir, "bundle.zip")
        self.set_status("uploading", f"uploading bundle ({self.bundle_bytes / 1e6:.1f} MB) to R2")
        last = [0.0]

        def progress(sent, total):
            frac = sent / total if total else 1.0
            with self.lock:
                self.upload_progress = frac
            if frac - last[0] >= 0.1 or frac >= 1.0:
                last[0] = frac
                self.log(f"upload {frac * 100:.0f}%")

        self.r2.upload_file(zip_path, f"{self.prefix}/bundle.zip", progress=progress, content_type="application/zip")
        self.r2.put_json(f"{self.prefix}/job.json", {
            "job_id": cfg.job_id,
            "created_at": self.started_at,
            "blend": cfg.blend_name,
            "blender_version": cfg.blender_version_string,
            "plan": [{k: v for k, v in p.items()} for p in cfg.plan],
            "manifest": self.manifest,
        })
        try:
            os.remove(zip_path)
        except OSError:
            pass

    def _worker_env(self, w: WorkerState, frames: Optional[list] = None) -> dict:
        cfg = self.cfg
        env = {
            "CR_JOB_ID": cfg.job_id,
            "CR_WORKER_INDEX": str(w.index),
            "CR_FRAME_START": str(w.frame_start),
            "CR_FRAME_END": str(w.frame_end),
            "CR_FRAME_STEP": str(w.frame_step),
            "CR_R2_ENDPOINT": self.r2.endpoint,
            "CR_R2_BUCKET": cfg.r2_bucket,
            "CR_R2_ACCESS_KEY": cfg.r2_access_key,
            "CR_R2_SECRET_KEY": cfg.r2_secret_key,
            "CR_R2_PREFIX": cfg.r2_prefix.strip("/"),
            "CR_BLENDER_VERSION": ".".join(str(x) for x in cfg.blender_version[:3]),
            "CR_DEVICE": cfg.device,
            "CR_AUTO_DESTROY": "1" if cfg.auto_destroy else "0",
            "NVIDIA_DRIVER_CAPABILITIES": "all",
        }
        if frames is not None:
            env["CR_FRAMES"] = ",".join(str(f) for f in frames)
        if cfg.auto_destroy:
            env["CR_VAST_API_KEY"] = cfg.vast_key
        return env

    def _find_offers(self, n: int, exclude_machines: set) -> list:
        cfg = self.cfg
        offers = self.vast.search_offers(
            min_vram_mb=cfg.min_vram_gb * 1024, disk_gb=cfg.disk_gb, max_dph=cfg.max_dph,
            min_reliability=cfg.min_reliability, min_driver=min_driver_for(cfg.blender_version),
            series=cfg.series, min_inet_down=cfg.min_inet_down,
        )
        offers = [o for o in offers if o.get("machine_id") not in exclude_machines]
        return VastClient.pick_offers(offers, n)

    def _create_worker(self, w: WorkerState, offer: dict, image: str, login: str, frames: Optional[list] = None) -> None:
        cfg = self.cfg
        label = f"cloudrender-{cfg.job_id[:8]}-w{w.index}-{uuid.uuid4().hex[:4]}"
        env = self._worker_env(w, frames)
        with self.lock:
            w.state = "creating"
            w.offer = {k: offer.get(k) for k in ("id", "machine_id", "gpu_name", "gpu_ram", "dph_total",
                                                  "driver_version", "geolocation", "reliability", "inet_down")}
            w.attempts += 1
            w.error = ""
            w.created_at = _now()
            w.last_status_at = ""
        iid = self.vast.create_instance(
            int(offer["id"]), image=image, env=env, onstart="bash /opt/worker/entrypoint.sh",
            disk_gb=cfg.disk_gb, label=label, image_login=login,
        )
        with self.lock:
            w.instance_id = iid
            w.state = "loading"
            self.instances_ever.add(iid)
            self.cost_per_hour = sum(float(x.offer.get("dph_total") or 0) for x in self.workers
                                     if x.state not in ("done", "failed", "dead", "pending"))
        self.log(f"worker {w.index}: instance {iid} on {offer.get('gpu_name')} @ ${float(offer.get('dph_total') or 0):.3f}/h "
                 f"({offer.get('geolocation')}, driver {offer.get('driver_version')}) frames {w.frame_start}-{w.frame_end}")
        self.save()

    def _stage_provision(self) -> None:
        cfg = self.cfg
        self.set_status("provisioning", f"searching Vast.ai for {len(self.workers)} cheapest RTX workers")
        image = resolve_image_digest(cfg.image, cfg.ghcr_user, cfg.ghcr_token) if cfg.pin_digest else cfg.image
        if image != cfg.image:
            self.log(f"image pinned to {image.split('@')[-1][:19]}...")
        login = ghcr_login_string(cfg.ghcr_user, cfg.ghcr_token)
        offers = self._find_offers(len(self.workers), set())
        if not offers:
            raise RuntimeError("no Vast.ai offers match the GPU/price/driver filters - relax the filters and retry")
        if len(offers) < len(self.workers):
            self.log(f"only {len(offers)} offers match; merging frame ranges onto fewer workers")
            frames = [f for w in self.workers for f in w.frames]
            plan = planner.split_frames(frames[0], frames[-1], cfg.frame_step, len(offers))
            with self.lock:
                self.workers = [WorkerState(index=p["index"], frames=p["frames"], frame_start=p["frame_start"],
                                            frame_end=p["frame_end"], frame_step=p["frame_step"]) for p in plan]
        for w, offer in zip(self.workers, offers):
            self._check_cancel()
            try:
                self._create_worker(w, offer, image, login)
            except VastError as exc:
                self.log(f"worker {w.index}: create failed on offer {offer.get('id')}: {exc}")
                with self.lock:
                    w.failed_machines.append(offer.get("machine_id"))
                    w.state = "pending"
                # try the next cheapest offer once more right away
                alt = self._find_offers(1, {o.get("machine_id") for o in offers} | set(w.failed_machines))
                if alt:
                    self._create_worker(w, alt[0], image, login)
                else:
                    raise
        self.r2.put_json(f"{self.prefix}/workers.json", {
            str(w.index): {"instance_id": w.instance_id, "frames": w.frames} for w in self.workers
        })
        with self.lock:
            self.render_started_at = _now()
        self.set_status("rendering", f"{len(self.workers)} workers starting (image pull + Blender download takes a few minutes)")

    # -------------------------------------------------------------- monitor
    def _monitor(self) -> None:
        cfg = self.cfg
        image = None
        login = ghcr_login_string(cfg.ghcr_user, cfg.ghcr_token)
        tick = 0
        while True:
            self._check_cancel()
            tick += 1
            try:
                self._poll_frames()
                self._poll_worker_status()
                if tick % 3 == 1:
                    self._poll_instances()
            except (R2Error, VastError) as exc:
                self.log(f"poll error: {exc}")
            if cfg.auto_download:
                try:
                    self._download_new_frames()
                except Exception as exc:
                    self.log(f"download error: {exc}")

            # retries for dead/failed workers
            for w in list(self.workers):
                if w.state in ("dead", "failed") and w.remaining:
                    if w.attempts > cfg.max_retries:
                        continue
                    if image is None:
                        image = resolve_image_digest(cfg.image, cfg.ghcr_user, cfg.ghcr_token) if cfg.pin_digest else cfg.image
                    self._retry_worker(w, image, login)

            with self.lock:
                all_frames_present = all(self._frame_present(w, f) for w in self.workers for f in w.frames)
                unrecoverable = [w for w in self.workers if w.state in ("dead", "failed") and w.remaining
                                 and w.attempts > cfg.max_retries]
                active = [w for w in self.workers if w.state not in ("done", "dead", "failed")]
                done = len(self.frames_in_r2)
                self.message = f"{done}/{self.frames_total} frames rendered, {len(active)} workers active"
            self.save()
            if all_frames_present:
                self._finish()
                return
            if unrecoverable and not active:
                missing = sorted(f for w in unrecoverable for f in w.remaining)
                self._teardown("failed", f"{len(missing)} frames could not be rendered after retries: "
                                         f"{missing[:10]}{'...' if len(missing) > 10 else ''}")
                return
            self.cancel_event.wait(cfg.poll_interval)

    def _frame_present(self, w: WorkerState, frame: int) -> bool:
        name = self.frame_filenames.get(str(frame))
        if name is None:
            return w.state == "done" or frame in w.frames_done
        return name in self.frames_in_r2

    def _poll_frames(self) -> None:
        names = set()
        for obj in self.r2.list(f"{self.prefix}/frames/"):
            rel = obj["key"][len(self.prefix) + len("/frames/"):]
            if rel and not rel.endswith("/"):
                names.add(rel)
        with self.lock:
            self.frames_in_r2 = names
            if not self.frame_filenames:
                return
            for w in self.workers:
                done = [f for f in w.frames if self.frame_filenames.get(str(f)) in names]
                w.frames_done = done
                if w.state not in ("failed", "dead") and done and len(done) == len(w.frames):
                    w.state = "done"

    def _poll_worker_status(self) -> None:
        for w in self.workers:
            if w.state in ("done", "pending", "dead", "failed") or w.instance_id is None:
                continue
            st = self.r2.get_json(f"{self.prefix}/status/worker_{w.index}.json")
            if not st:
                continue
            reported = str(st.get("instance_id") or "")
            if reported and reported != str(w.instance_id):
                continue  # stale status written by a previous instance of this worker index
            with self.lock:
                w.last_status_at = st.get("updated_at", "")
                if not self.frame_filenames:
                    w.frames_done = [f for f in (st.get("frames_done") or []) if f in w.frames]
                w.progress = st.get("progress", "") or ""
                w.current_frame = st.get("current_frame")
                w.device_used = st.get("device_used")
                w.gpu = st.get("gpu", "") or ""
                state = st.get("state", "")
                if state == "failed":
                    w.state = "failed"
                    w.error = st.get("error", "") or "worker reported failure"
                    self.log(f"worker {w.index} FAILED: {w.error[:200]}")
                elif state == "done":
                    if w.remaining:
                        # worker says done but frames missing -> treat as failure so it is retried
                        w.state = "failed"
                        w.error = "worker finished but frames are missing in R2"
                    else:
                        w.state = "done"
                elif state in ("starting", "downloading", "rendering", "uploading"):
                    w.state = state

    def _poll_instances(self) -> None:
        instances = {int(i["id"]): i for i in self.vast.list_instances() if i.get("id") is not None}
        now = _dt.datetime.now(_dt.timezone.utc)
        with self.lock:
            for w in self.workers:
                if w.instance_id is None or w.state in ("done", "dead", "failed", "pending"):
                    continue
                inst = instances.get(int(w.instance_id))
                if inst is None:
                    if w.remaining:
                        w.state = "dead"
                        w.error = "instance disappeared"
                        self.log(f"worker {w.index}: instance {w.instance_id} is gone")
                    else:
                        w.state = "done"
                    continue
                actual = (inst.get("actual_status") or "").lower()
                msg = (inst.get("status_msg") or "")[:160]
                if actual in DEAD_INSTANCE_STATES:
                    if w.remaining:
                        w.state = "dead"
                        w.error = f"instance {actual}: {msg}"
                        self.log(f"worker {w.index}: instance {w.instance_id} {actual} - {msg}")
                    else:
                        w.state = "done"
                    continue
                # stuck pulling the image / never reported
                created = _dt.datetime.fromisoformat(w.created_at) if w.created_at else now
                age_min = (now - created).total_seconds() / 60.0
                if not w.last_status_at and age_min > self.cfg.loading_timeout_minutes:
                    w.state = "dead"
                    w.error = f"no heartbeat after {age_min:.0f} min (status={actual}: {msg})"
                    self.log(f"worker {w.index}: {w.error}")
                    continue
                if w.last_status_at:
                    last = _dt.datetime.fromisoformat(w.last_status_at)
                    if (now - last).total_seconds() / 60.0 > self.cfg.stale_minutes:
                        w.state = "dead"
                        w.error = f"heartbeat stale for {(now - last).total_seconds() / 60:.0f} min"
                        self.log(f"worker {w.index}: {w.error}")

    def _retry_worker(self, w: WorkerState, image: str, login: str) -> None:
        old = w.instance_id
        if old is not None:
            self.log(f"worker {w.index}: destroying instance {old} before retry")
            try:
                self.vast.destroy_instance(int(old))
            except VastError as exc:
                self.log(f"destroy {old} failed: {exc}")
        with self.lock:
            if w.offer.get("machine_id") is not None:
                w.failed_machines.append(w.offer.get("machine_id"))
            exclude = set(w.failed_machines)
            remaining = list(w.remaining)
        offers = self._find_offers(1, exclude)
        if not offers:
            self.log(f"worker {w.index}: no alternative offers for retry")
            with self.lock:
                w.attempts = self.cfg.max_retries + 1
            return
        self.log(f"worker {w.index}: retry {w.attempts}/{self.cfg.max_retries} with {len(remaining)} remaining frames")
        try:
            self._create_worker(w, offers[0], image, login, frames=remaining)
        except VastError as exc:
            self.log(f"worker {w.index}: retry create failed: {exc}")
            with self.lock:
                w.state = "dead"
        self.r2.put_json(f"{self.prefix}/workers.json", {
            str(x.index): {"instance_id": x.instance_id, "frames": x.frames} for x in self.workers
        })

    def _download_new_frames(self) -> None:
        with self.lock:
            todo = sorted(self.frames_in_r2 - self.downloaded)
        if not todo:
            return
        os.makedirs(self.cfg.output_dir, exist_ok=True)
        for rel in todo:
            dest = os.path.join(self.cfg.output_dir, "cloud_extra", *rel.split("/")[1:]) if rel.startswith("_extra/") \
                else os.path.join(self.cfg.output_dir, rel)
            self.r2.download_file(f"{self.prefix}/frames/{rel}", dest)
            with self.lock:
                self.downloaded.add(rel)
        self.log(f"downloaded {len(todo)} file(s) -> {self.cfg.output_dir}")

    # ---------------------------------------------------------------- end
    def _finish(self) -> None:
        self.set_status("finishing", "all frames rendered - downloading and shutting down workers")
        if self.cfg.auto_download:
            for _ in range(3):
                try:
                    self._download_new_frames()
                    break
                except Exception as exc:
                    self.log(f"download retry: {exc}")
                    time.sleep(5)
        self._destroy_all()
        if not self.cfg.keep_bundle_in_r2:
            try:
                self.r2.delete(f"{self.prefix}/bundle.zip")
            except R2Error:
                pass
        with self.lock:
            self.finished_at = _now()
        self.set_status("done", f"{self.frames_total} frames rendered, est. cost ${self.cost_so_far():.2f}")

    def _teardown(self, status: str, message: str) -> None:
        try:
            self._destroy_all()
        except Exception as exc:
            self.log(f"teardown error: {exc}")
        with self.lock:
            self.finished_at = _now()
        self.set_status(status, message)

    def _destroy_all(self) -> None:
        with self.lock:
            ids = sorted({int(w.instance_id) for w in self.workers if w.instance_id is not None})
        if not ids:
            return
        try:
            live = {int(i["id"]) for i in self.vast.list_instances() if i.get("id") is not None}
        except VastError:
            live = set(ids)
        targets = [i for i in ids if i in live]
        if not targets:
            self.log("all workers already gone")
            return
        self.log(f"destroying {len(targets)} instance(s): {targets}")
        remaining = self.vast.destroy_many(targets)
        if remaining:
            self.log(f"WARNING: instances still alive after destroy attempts: {remaining} - check the Vast console")
        else:
            self.log("all instances confirmed destroyed")

    def _cleanup_local(self) -> None:
        for p in (self.cfg.tmp_blend, os.path.join(self.cfg.job_dir, "bundle.zip")):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        shutil.rmtree(os.path.join(self.cfg.job_dir, "bundle"), ignore_errors=True)


class _Cancelled(Exception):
    pass
