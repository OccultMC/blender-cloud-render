"""Per-scene settings shown in the Render properties panel."""
from __future__ import annotations

from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import PropertyGroup


class CloudRenderSettings(PropertyGroup):
    enabled: BoolProperty(
        name="Render on Cloud", default=False,
        description="Render Animation on Vast.ai GPU workers (Render Image always stays on this machine)",
    )
    worker_count: IntProperty(
        name="Number of Workers", default=4, min=0, max=20,
        description="How many Vast.ai machines to rent. Frames are split evenly (whole frames only). 0 renders locally",
    )
    series_20: BoolProperty(name="RTX 20", default=True)
    series_30: BoolProperty(name="RTX 30", default=True)
    series_40: BoolProperty(name="RTX 40", default=True)
    series_50: BoolProperty(name="RTX 50", default=True)
    min_vram_gb: IntProperty(name="Min VRAM", default=8, min=4, max=48, subtype="UNSIGNED",
                             description="Minimum GPU memory in GB (the scene must fit on the card)")
    max_price: FloatProperty(name="Max $/hour", default=0.60, min=0.0, soft_max=5.0, precision=3,
                             description="Maximum hourly price per worker (0 = no limit)")
    min_reliability: FloatProperty(name="Min Reliability", default=0.95, min=0.5, max=1.0, precision=2,
                                   description="Vast.ai host reliability score")
    disk_gb: IntProperty(name="Disk (GB)", default=40, min=10, max=500,
                         description="Container disk for Blender, the bundle and rendered frames")
    min_inet_down: IntProperty(name="Min Download Mbps", default=200, min=10, max=5000)
    auto_download: BoolProperty(name="Download frames as they finish", default=True,
                                description="Pull finished frames from R2 into the scene's output folder")
    auto_destroy: BoolProperty(name="Workers self-destroy", default=True,
                               description="Workers delete their own Vast instance when done (the add-on also cleans up)")
    keep_bundle: BoolProperty(name="Keep bundle in R2", default=False,
                              description="Do not delete the uploaded .blend bundle after the job")
    max_retries: IntProperty(name="Retries per worker", default=2, min=0, max=5,
                             description="Re-dispatch a worker's remaining frames on a new machine if it dies")
    show_advanced: BoolProperty(name="Advanced", default=False)
    active_job_id: StringProperty(name="Active Job", default="", options={"HIDDEN"})

    def selected_series(self) -> list:
        out = []
        if self.series_20:
            out.append("20")
        if self.series_30:
            out.append("30")
        if self.series_40:
            out.append("40")
        if self.series_50:
            out.append("50")
        return out
