"""Cloud Render - distribute Cycles animation renders across Vast.ai RTX workers.

Render Image stays on this machine.  Render Animation (menu or Ctrl+F12) packs the
whole scene, rents the cheapest N RTX 20/30/40/50 machines on Vast.ai, renders a
whole-frame slice per worker with OptiX, and streams the image sequence back
through Cloudflare R2 into the scene's output folder.
"""
from __future__ import annotations

import os

import bpy
from bpy.app.handlers import persistent

from . import ops, prefs, props, state, ui

_CLASSES = (prefs.CLOUDRENDER_preferences, props.CloudRenderSettings) + ops.CLASSES + ui.CLASSES


@persistent
def _on_load_post(_dummy=None):
    """Reconnect to a still-running job recorded in the freshly loaded file."""
    try:
        scene = bpy.context.scene
        s = getattr(scene, "cloud_render", None)
        if not s or not s.active_job_id or not bpy.data.filepath:
            return
        job_dir = os.path.join(ops.jobs_root(), s.active_job_id)
        if not os.path.exists(os.path.join(job_dir, "job.json")):
            return
        creds = prefs.resolve_credentials(prefs.get_prefs())
        if creds.missing():
            return
        from .job import CloudJob
        job = CloudJob.load(job_dir, ops.secrets_from_creds(creds))
        state.set_job(job)
        if job.status not in ("done", "failed", "cancelled"):
            job.start()
    except Exception as exc:  # never break file loading
        print(f"[cloud_render] load_post: {exc}")


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cloud_render = bpy.props.PointerProperty(type=props.CloudRenderSettings)
    ui.install_menu_override()
    ui.install_keymap()
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    state.stop_ui_timer()
    ui.remove_keymap()
    ui.remove_menu_override()
    if hasattr(bpy.types.Scene, "cloud_render"):
        del bpy.types.Scene.cloud_render
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
