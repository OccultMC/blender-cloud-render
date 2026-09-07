"""Render-properties panel, Render menu override and Ctrl+F12 keymap."""
from __future__ import annotations

import bpy
from bpy.types import Panel

from . import state
from .ops import cloud_enabled
from .planner import describe_plan, split_frames

STATE_ICONS = {
    "pending": "RADIOBUT_OFF", "creating": "TIME", "loading": "TIME", "starting": "TIME",
    "downloading": "IMPORT", "rendering": "RENDER_ANIMATION", "uploading": "EXPORT",
    "done": "CHECKMARK", "failed": "ERROR", "dead": "CANCEL",
}


class CloudRenderPanelMixin:
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"
    COMPAT_ENGINES = {"CYCLES"}

    @classmethod
    def poll(cls, context):
        return context.engine in cls.COMPAT_ENGINES


class CLOUDRENDER_PT_main(CloudRenderPanelMixin, Panel):
    bl_label = "Render on Cloud"
    bl_order = 5

    def draw_header(self, context):
        self.layout.prop(context.scene.cloud_render, "enabled", text="")

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        s = scene.cloud_render
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.active = s.enabled

        if not s.enabled:
            layout.label(text="Render Animation on Vast.ai RTX workers; Render Image stays local.", icon="INFO")
            return

        col = layout.column(align=True)
        col.prop(s, "worker_count")
        frames = split_frames(scene.frame_start, scene.frame_end, scene.frame_step, max(1, s.worker_count))
        if s.worker_count == 0:
            col.label(text="0 workers: Render Animation renders locally", icon="INFO")
        else:
            col.label(text=describe_plan(frames), icon="SEQUENCE")

        row = layout.row(align=True, heading="GPU Series")
        row.prop(s, "series_20", toggle=True)
        row.prop(s, "series_30", toggle=True)
        row.prop(s, "series_40", toggle=True)
        row.prop(s, "series_50", toggle=True)

        col = layout.column(align=True)
        col.prop(s, "min_vram_gb")
        col.prop(s, "max_price")

        row = layout.row(align=True)
        row.operator("cloudrender.preview_workers", icon="VIEWZOOM")
        job = state.ACTIVE_JOB
        running = job is not None and job.is_running
        sub = row.row(align=True)
        sub.enabled = not running and s.worker_count > 0
        sub.operator("cloudrender.render_animation", text="Render Animation on Cloud", icon="RENDER_ANIMATION")

        if state.OFFERS_PREVIEW_MSG:
            box = layout.box()
            box.label(text=state.OFFERS_PREVIEW_MSG, icon="WORLD")
            for o in state.OFFERS_PREVIEW:
                box.label(text=f"{o['gpu']} {o['vram']}GB  ${o['dph']:.3f}/h  {o['geo']}  drv {o['driver']}  rel {o['rel']:.2f}")


class CLOUDRENDER_PT_advanced(CloudRenderPanelMixin, Panel):
    bl_label = "Worker Options"
    bl_parent_id = "CLOUDRENDER_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return super().poll(context) and context.scene.cloud_render.enabled

    def draw(self, context):
        layout = self.layout
        s = context.scene.cloud_render
        layout.use_property_split = True
        layout.use_property_decorate = False
        col = layout.column(align=True)
        col.prop(s, "min_reliability")
        col.prop(s, "min_inet_down")
        col.prop(s, "disk_gb")
        col.prop(s, "max_retries")
        col = layout.column(align=True)
        col.prop(s, "auto_download")
        col.prop(s, "auto_destroy")
        col.prop(s, "keep_bundle")
        layout.operator("cloudrender.destroy_all", icon="TRASH")


class CLOUDRENDER_PT_job(CloudRenderPanelMixin, Panel):
    bl_label = "Cloud Job"
    bl_parent_id = "CLOUDRENDER_PT_main"

    @classmethod
    def poll(cls, context):
        if not super().poll(context):
            return False
        s = context.scene.cloud_render
        return s.enabled and (state.ACTIVE_JOB is not None or bool(s.active_job_id))

    def draw(self, context):
        layout = self.layout
        s = context.scene.cloud_render
        job = state.ACTIVE_JOB
        snap = state.SNAPSHOT if job is not None else {}
        if job is None:
            layout.label(text=f"Job {s.active_job_id} recorded in this file", icon="FILE_TICK")
            row = layout.row(align=True)
            row.operator("cloudrender.resume_job", icon="FILE_REFRESH")
            row.operator("cloudrender.clear_job", icon="X")
            return

        status = snap.get("status", "")
        icon = {"done": "CHECKMARK", "failed": "ERROR", "cancelled": "CANCEL"}.get(status, "TIME")
        layout.label(text=f"{status.upper()}  -  {snap.get('message', '')}"[:110], icon=icon)
        total = snap.get("frames_total", 0)
        done = snap.get("frames_done", 0)
        if status == "uploading":
            layout.progress(factor=snap.get("upload_progress", 0.0), type="BAR",
                            text=f"Uploading bundle {snap.get('upload_progress', 0) * 100:.0f}%")
        else:
            layout.progress(factor=snap.get("progress", 0.0), type="BAR", text=f"{done}/{total} frames")
        if snap.get("cost_per_hour") or snap.get("cost_so_far"):
            layout.label(text=f"${snap.get('cost_per_hour', 0):.3f}/hour  -  est. ${snap.get('cost_so_far', 0):.2f} so far",
                         icon="FUND")
        if snap.get("frames_downloaded"):
            layout.label(text=f"{snap['frames_downloaded']} files downloaded to {snap.get('output_dir', '')}"[:110],
                         icon="IMPORT")

        box = layout.box()
        for w in snap.get("workers", []):
            row = box.row(align=True)
            gpu = (w.get("offer") or {}).get("gpu_name") or "-"
            dph = float((w.get("offer") or {}).get("dph_total") or 0)
            label = f"W{w['index']}  {w['frame_start']}-{w['frame_end']}  {len(w.get('frames_done', []))}/{len(w['frames'])}"
            row.label(text=label, icon=STATE_ICONS.get(w.get("state"), "DOT"))
            detail = f"{w.get('state')}  {gpu}  ${dph:.3f}/h"
            if w.get("device_used"):
                detail += f"  {w['device_used']}"
            if w.get("current_frame") is not None and w.get("state") == "rendering":
                detail += f"  fr {w['current_frame']}: {w.get('progress', '')[:40]}"
            if w.get("error"):
                detail += f"  ! {w['error'][:60]}"
            row.label(text=detail[:120])

        warnings = snap.get("warnings", [])
        if warnings:
            wb = layout.box()
            wb.label(text=f"{len(warnings)} packing warning(s)", icon="ERROR")
            for wmsg in warnings[:4]:
                wb.label(text=wmsg[:115])
        if snap.get("error"):
            layout.label(text=snap["error"][:115], icon="ERROR")

        row = layout.row(align=True)
        if job.is_running:
            row.operator("cloudrender.cancel_job", icon="CANCEL")
        else:
            row.operator("cloudrender.clear_job", icon="X")
        row.operator("cloudrender.download_frames", icon="IMPORT")
        row.operator("cloudrender.open_output", icon="FILE_FOLDER")
        row.operator("cloudrender.show_log", text="Log", icon="TEXT")
        for line in snap.get("log_tail", [])[-4:]:
            layout.label(text=line[:115])


# --------------------------------------------------------------------------- #
# Render menu override: swap "Render Animation" for the cloud operator
# --------------------------------------------------------------------------- #

_original_render_menu_draw = None


def _draw_render_menu(self, context):
    layout = self.layout
    scene = context.scene
    rd = scene.render
    s = getattr(scene, "cloud_render", None)
    cloud = cloud_enabled(scene)

    layout.operator("render.render", text="Render Image", icon="RENDER_STILL").use_viewport = True
    if cloud:
        layout.operator("cloudrender.render_animation",
                        text=f"Render Animation on Cloud ({s.worker_count} workers)", icon="RENDER_ANIMATION")
        props = layout.operator("render.render", text="Render Animation Locally", icon="RENDER_ANIMATION")
    else:
        props = layout.operator("render.render", text="Render Animation", icon="RENDER_ANIMATION")
    props.animation = True
    props.use_viewport = True

    layout.separator()

    # Blender 5.x: separate sequencer scene entries.
    seq_scene = getattr(context, "sequencer_scene", None)
    strips = getattr(context, "strips", ())
    if seq_scene and seq_scene.render.use_sequencer and strips and seq_scene != scene:
        props = layout.operator("render.render", text="Render Sequencer Image", icon="RENDER_STILL")
        props.use_viewport = True
        props.use_sequencer_scene = True
        props = layout.operator("render.render", text="Render Sequencer Animation", icon="RENDER_ANIMATION")
        props.animation = True
        props.use_viewport = True
        props.use_sequencer_scene = True
        layout.separator()

    layout.operator("sound.mixdown", text="Render Audio...")
    layout.separator()
    layout.operator("render.view_show", text="View Render")
    layout.operator("render.play_rendered_anim", text="View Animation")
    layout.separator()
    layout.prop(rd, "use_lock_interface", text="Lock Interface")


def install_menu_override():
    global _original_render_menu_draw
    menu = bpy.types.TOPBAR_MT_render
    if _original_render_menu_draw is None:
        _original_render_menu_draw = menu.draw
    menu.draw = _draw_render_menu


def remove_menu_override():
    global _original_render_menu_draw
    if _original_render_menu_draw is not None:
        bpy.types.TOPBAR_MT_render.draw = _original_render_menu_draw
        _original_render_menu_draw = None


# --------------------------------------------------------------------------- #
# keymap: Ctrl+F12 -> cloud-aware Render Animation
# --------------------------------------------------------------------------- #

_keymaps = []


def install_keymap():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        return
    km = kc.keymaps.new(name="Screen", space_type="EMPTY")
    kmi = km.keymap_items.new("cloudrender.render_animation", "F12", "PRESS", ctrl=True)
    _keymaps.append((km, kmi))


def remove_keymap():
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _keymaps.clear()


CLASSES = (CLOUDRENDER_PT_main, CLOUDRENDER_PT_advanced, CLOUDRENDER_PT_job)
