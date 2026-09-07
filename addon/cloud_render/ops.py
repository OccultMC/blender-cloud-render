"""Operators: dispatch a cloud render, preview offers, cancel, resume, tests."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import bpy
from bpy.props import StringProperty
from bpy.types import Operator

from . import packer, planner, state
from .job import CloudJob, JobConfig
from .prefs import get_prefs, load_env_file, resolve_credentials
from .r2 import R2Client
from .vast import VastClient, VastError, min_driver_for

JOBS_DIRNAME = "cloud_render_jobs"


# --------------------------------------------------------------------------- #
# helpers (main thread)
# --------------------------------------------------------------------------- #

def cloud_enabled(scene) -> bool:
    s = getattr(scene, "cloud_render", None)
    return bool(s and s.enabled and scene.render.engine == "CYCLES" and s.worker_count > 0)


def output_dir_for(scene) -> str:
    fp = scene.render.filepath or "//render/"
    ab = bpy.path.abspath(fp)
    if fp.endswith(("/", "\\")) or os.path.isdir(ab):
        return os.path.normpath(ab)
    return os.path.normpath(os.path.dirname(ab) or bpy.path.abspath("//"))


def jobs_root() -> str:
    return os.path.join(os.path.dirname(bpy.data.filepath), JOBS_DIRNAME)


def secrets_from_creds(creds) -> dict:
    return {
        "vast_key": creds.vast_key,
        "r2_access_key": creds.r2_access_key,
        "r2_secret_key": creds.r2_secret_key,
        "ghcr_token": creds.ghcr_token,
    }


def validate(context) -> str:
    scene = context.scene
    if not bpy.data.filepath:
        return "Save the .blend file first"
    if scene.render.engine != "CYCLES":
        return "Cloud rendering is only available for Cycles"
    if not getattr(bpy.app, "online_access", True):
        return "Enable 'Allow Online Access' in Preferences > System"
    if scene.render.image_settings.file_format == "FFMPEG":
        return "Output format is a video; cloud rendering produces an image sequence - pick PNG/EXR/etc."
    if scene.frame_end < scene.frame_start:
        return "Frame End is before Frame Start"
    job = state.ACTIVE_JOB
    if job is not None and job.is_running:
        return "A cloud render job is already running - cancel it first"
    creds = resolve_credentials(get_prefs(context))
    missing = creds.missing()
    if missing:
        return "Missing credentials in add-on preferences: " + ", ".join(missing)
    return ""


def build_config(context, tmp_blend: str) -> JobConfig:
    scene = context.scene
    s = scene.cloud_render
    creds = resolve_credentials(get_prefs(context))
    blend_path = bpy.data.filepath
    blend_dir = os.path.dirname(blend_path)
    name = os.path.splitext(os.path.basename(blend_path))[0]
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    plan = planner.split_frames(scene.frame_start, scene.frame_end, scene.frame_step, s.worker_count)
    return JobConfig(
        job_id=job_id, blend_path=blend_path, blend_name=name, blend_dir=blend_dir,
        job_dir=os.path.join(jobs_root(), job_id), output_dir=output_dir_for(scene),
        tmp_blend=tmp_blend, blender_exe=bpy.app.binary_path, blender_version=tuple(bpy.app.version),
        blender_version_string=bpy.app.version_string, plan=plan, frame_step=max(1, scene.frame_step),
        image=creds.image, ghcr_user=creds.ghcr_user, ghcr_token=creds.ghcr_token, pin_digest=creds.pin_digest,
        vast_key=creds.vast_key, r2_account=creds.r2_account, r2_access_key=creds.r2_access_key,
        r2_secret_key=creds.r2_secret_key, r2_bucket=creds.r2_bucket, r2_endpoint=creds.r2_endpoint,
        r2_prefix=creds.r2_prefix, series=s.selected_series(), min_vram_gb=s.min_vram_gb, disk_gb=s.disk_gb,
        max_dph=s.max_price, min_reliability=s.min_reliability, min_inet_down=s.min_inet_down,
        auto_download=s.auto_download, auto_destroy=s.auto_destroy, max_retries=s.max_retries,
        keep_bundle_in_r2=s.keep_bundle,
    )


# --------------------------------------------------------------------------- #
# operators
# --------------------------------------------------------------------------- #

class CLOUDRENDER_OT_render_animation(Operator):
    """Render the animation. With 'Render on Cloud' enabled the frames are split across Vast.ai workers"""
    bl_idname = "cloudrender.render_animation"
    bl_label = "Render Animation"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        scene = context.scene
        if not cloud_enabled(scene):
            return bpy.ops.render.render("INVOKE_DEFAULT", animation=True, use_viewport=True)
        err = validate(context)
        if err:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        s = scene.cloud_render
        if not s.selected_series():
            self.report({"ERROR"}, "Select at least one RTX series")
            return {"CANCELLED"}
        try:
            tmp_blend = packer.snapshot_session(bpy.data.filepath)
        except Exception as exc:
            self.report({"ERROR"}, f"Could not snapshot the session: {exc}")
            return {"CANCELLED"}
        cfg = build_config(context, tmp_blend)
        if not cfg.plan:
            self.report({"ERROR"}, "No frames to render")
            return {"CANCELLED"}
        job = CloudJob(cfg)
        s.active_job_id = cfg.job_id
        state.set_job(job)
        job.log(f"job {cfg.job_id}: {planner.describe_plan(cfg.plan)}; Blender {cfg.blender_version_string}; "
                f"OptiX needs driver >= {min_driver_for(cfg.blender_version)}")
        job.start()
        self.report({"INFO"}, f"Cloud render started: {planner.describe_plan(cfg.plan)}")
        return {"FINISHED"}


class CLOUDRENDER_OT_preview_workers(Operator):
    """Search Vast.ai for the cheapest matching GPUs without renting anything"""
    bl_idname = "cloudrender.preview_workers"
    bl_label = "Find Workers"

    def execute(self, context):
        scene = context.scene
        s = scene.cloud_render
        creds = resolve_credentials(get_prefs(context))
        if not creds.vast_key:
            self.report({"ERROR"}, "Vast.ai API key missing (add-on preferences)")
            return {"CANCELLED"}
        try:
            client = VastClient(creds.vast_key)
            offers = client.search_offers(
                min_vram_mb=s.min_vram_gb * 1024, disk_gb=s.disk_gb, max_dph=s.max_price,
                min_reliability=s.min_reliability, min_driver=min_driver_for(tuple(bpy.app.version)),
                series=s.selected_series(), min_inet_down=s.min_inet_down,
            )
        except VastError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        n = max(1, s.worker_count)
        picked = VastClient.pick_offers(offers, n)
        state.OFFERS_PREVIEW = [{
            "gpu": (o.get("gpu_name") or "").replace("_", " "),
            "vram": round((o.get("gpu_ram") or 0) / 1024),
            "dph": float(o.get("dph_total") or 0),
            "geo": o.get("geolocation") or "",
            "driver": str(o.get("driver_version") or ""),
            "rel": float(o.get("reliability") or 0),
        } for o in picked]
        total = sum(p["dph"] for p in state.OFFERS_PREVIEW)
        state.OFFERS_PREVIEW_MSG = (
            f"{len(offers)} matching offers; cheapest {len(picked)} = ${total:.3f}/hour total"
            if picked else "No offers match - relax VRAM/price/series filters"
        )
        self.report({"INFO"}, state.OFFERS_PREVIEW_MSG)
        state.redraw_properties()
        return {"FINISHED"}


class CLOUDRENDER_OT_cancel_job(Operator):
    """Stop the cloud job and destroy all its Vast.ai instances"""
    bl_idname = "cloudrender.cancel_job"
    bl_label = "Cancel Cloud Render"

    def execute(self, context):
        job = state.ACTIVE_JOB
        if job is None:
            self.report({"WARNING"}, "No active job")
            return {"CANCELLED"}
        job.cancel()
        self.report({"INFO"}, "Cancelling - instances are being destroyed")
        return {"FINISHED"}


class CLOUDRENDER_OT_resume_job(Operator):
    """Reconnect to the job recorded in this file and keep monitoring / downloading"""
    bl_idname = "cloudrender.resume_job"
    bl_label = "Resume Monitoring"

    def execute(self, context):
        s = context.scene.cloud_render
        if not s.active_job_id or not bpy.data.filepath:
            self.report({"WARNING"}, "No job recorded in this file")
            return {"CANCELLED"}
        job_dir = os.path.join(jobs_root(), s.active_job_id)
        if not os.path.exists(os.path.join(job_dir, "job.json")):
            self.report({"WARNING"}, f"job.json not found in {job_dir}")
            return {"CANCELLED"}
        creds = resolve_credentials(get_prefs(context))
        if creds.missing():
            self.report({"ERROR"}, "Missing credentials: " + ", ".join(creds.missing()))
            return {"CANCELLED"}
        try:
            job = CloudJob.load(job_dir, secrets_from_creds(creds))
        except Exception as exc:
            self.report({"ERROR"}, f"Could not load job: {exc}")
            return {"CANCELLED"}
        state.set_job(job)
        if job.status in ("done", "failed", "cancelled"):
            self.report({"INFO"}, f"Job already {job.status}")
            return {"FINISHED"}
        job.start()
        self.report({"INFO"}, "Resumed monitoring job " + job.cfg.job_id)
        return {"FINISHED"}


class CLOUDRENDER_OT_download_frames(Operator):
    """Download every frame the job has in R2 into the output folder"""
    bl_idname = "cloudrender.download_frames"
    bl_label = "Download Frames"

    def execute(self, context):
        job = state.ACTIVE_JOB
        if job is None:
            self.report({"WARNING"}, "No job loaded")
            return {"CANCELLED"}
        try:
            job._poll_frames()
            job._download_new_frames()
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"{len(job.downloaded)} files in {job.cfg.output_dir}")
        return {"FINISHED"}


class CLOUDRENDER_OT_open_output(Operator):
    """Open the output folder in the file browser"""
    bl_idname = "cloudrender.open_output"
    bl_label = "Open Output Folder"

    def execute(self, context):
        job = state.ACTIVE_JOB
        path = job.cfg.output_dir if job else output_dir_for(context.scene)
        os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return {"FINISHED"}


class CLOUDRENDER_OT_clear_job(Operator):
    """Forget the finished job so the panel goes back to its idle state"""
    bl_idname = "cloudrender.clear_job"
    bl_label = "Clear"

    def execute(self, context):
        job = state.ACTIVE_JOB
        if job is not None and job.is_running:
            self.report({"WARNING"}, "Job still running - cancel it first")
            return {"CANCELLED"}
        state.set_job(None)
        context.scene.cloud_render.active_job_id = ""
        state.redraw_properties()
        return {"FINISHED"}


class CLOUDRENDER_OT_destroy_all(Operator):
    """Emergency: destroy every Vast.ai instance labelled cloudrender-* on this account"""
    bl_idname = "cloudrender.destroy_all"
    bl_label = "Destroy All Cloud Render Instances"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        creds = resolve_credentials(get_prefs(context))
        try:
            client = VastClient(creds.vast_key)
            ids = [int(i["id"]) for i in client.list_instances()
                   if str(i.get("label") or "").startswith("cloudrender-")]
            left = client.destroy_many(ids)
        except VastError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if left:
            self.report({"WARNING"}, f"Still alive after destroy: {left}")
        else:
            self.report({"INFO"}, f"Destroyed {len(ids)} instance(s)")
        return {"FINISHED"}


class CLOUDRENDER_OT_show_log(Operator):
    """Show the job log"""
    bl_idname = "cloudrender.show_log"
    bl_label = "Cloud Render Log"

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=700)

    def execute(self, context):
        return {"FINISHED"}

    def draw(self, context):
        job = state.ACTIVE_JOB
        col = self.layout.column(align=True)
        if job is None:
            col.label(text="No job")
            return
        for line in job.log_lines[-40:]:
            col.label(text=line[:120])


class CLOUDRENDER_OT_test_vast(Operator):
    """Check the Vast.ai API key"""
    bl_idname = "cloudrender.test_vast"
    bl_label = "Test Vast.ai"

    def execute(self, context):
        creds = resolve_credentials(get_prefs(context))
        try:
            me = VastClient(creds.vast_key).whoami()
        except VastError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Vast.ai OK: {me.get('email') or me.get('username') or me.get('id')} "
                              f"credit ${float(me.get('credit') or 0):.2f}")
        return {"FINISHED"}


class CLOUDRENDER_OT_test_r2(Operator):
    """Check the R2 credentials by listing the bucket"""
    bl_idname = "cloudrender.test_r2"
    bl_label = "Test R2"

    def execute(self, context):
        creds = resolve_credentials(get_prefs(context))
        try:
            msg = R2Client(creds.r2_account, creds.r2_access_key, creds.r2_secret_key, creds.r2_bucket,
                           creds.r2_endpoint).test()
        except Exception as exc:
            self.report({"ERROR"}, f"R2: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, "R2 " + msg)
        return {"FINISHED"}


class CLOUDRENDER_OT_load_env(Operator):
    """Fill the preferences from a .env file (VAST_API_KEY, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, ...)"""
    bl_idname = "cloudrender.load_env"
    bl_label = "Import .env"

    def execute(self, context):
        prefs = get_prefs(context)
        path = bpy.path.abspath(prefs.env_file)
        try:
            values = load_env_file(path)
        except OSError as exc:
            self.report({"ERROR"}, f"Cannot read {path}: {exc}")
            return {"CANCELLED"}
        mapping = {
            "VAST_API_KEY": "vast_api_key", "R2_ACCOUNT_ID": "r2_account_id",
            "R2_ACCESS_KEY_ID": "r2_access_key", "R2_SECRET_ACCESS_KEY": "r2_secret_key",
            "R2_BUCKET_NAME": "r2_bucket", "GHCR_PAT": "ghcr_token", "GHCR_USER": "ghcr_user",
        }
        n = 0
        for env_key, prop in mapping.items():
            if values.get(env_key):
                setattr(prefs, prop, values[env_key])
                n += 1
        self.report({"INFO"}, f"Imported {n} value(s) from {os.path.basename(path)}")
        return {"FINISHED"}


CLASSES = (
    CLOUDRENDER_OT_render_animation,
    CLOUDRENDER_OT_preview_workers,
    CLOUDRENDER_OT_cancel_job,
    CLOUDRENDER_OT_resume_job,
    CLOUDRENDER_OT_download_frames,
    CLOUDRENDER_OT_open_output,
    CLOUDRENDER_OT_clear_job,
    CLOUDRENDER_OT_destroy_all,
    CLOUDRENDER_OT_show_log,
    CLOUDRENDER_OT_test_vast,
    CLOUDRENDER_OT_test_r2,
    CLOUDRENDER_OT_load_env,
)
