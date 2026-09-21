"""Per-scene settings shown in the Render properties panel."""
from __future__ import annotations

from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import PropertyGroup


class CloudOfferItem(PropertyGroup):
    """One Vast.ai offer as shown in the offer list."""
    offer_id: IntProperty()
    machine_id: IntProperty()
    gpu: StringProperty()
    vram_gb: IntProperty()
    ram_gb: IntProperty()
    cpu_cores: IntProperty()
    cpu_name: StringProperty()
    num_gpus: IntProperty(default=1)
    dph: FloatProperty()
    dlperf: FloatProperty()
    score: FloatProperty()
    reliability: FloatProperty()
    inet_down: FloatProperty()
    geo: StringProperty()
    driver: StringProperty()
    disk_gb: IntProperty()


def _offer_index_changed(self, context):
    if 0 <= self.offer_index < len(self.offers):
        self.selected_offer_id = self.offers[self.offer_index].offer_id
        self.pick_strategy = "MANUAL"


def _min_gpus_changed(self, context):
    if self.max_gpus < self.min_gpus:
        self.max_gpus = self.min_gpus


def _max_gpus_changed(self, context):
    if self.min_gpus > self.max_gpus:
        self.min_gpus = self.max_gpus


class CloudRenderSettings(PropertyGroup):
    mode: EnumProperty(
        name="Mode", default="ANIMATION",
        items=[("ANIMATION", "Animation", "Split the frame range across N workers"),
               ("IMAGE", "Single Image", "Render the current frame on one worker")],
    )
    pick_strategy: EnumProperty(
        name="Pick", default="BEST",
        items=[("BEST", "Best value", "Highest render throughput per dollar (Vast dlperf / price)"),
               ("CHEAPEST", "Cheapest", "Lowest hourly price per machine"),
               ("CHEAPEST_GPU", "Cheapest per GPU", "Lowest hourly price per GPU - the best deal on multi-GPU machines"),
               ("FASTEST", "Fastest", "Highest raw benchmark score"),
               ("MANUAL", "Selected offer", "The offer highlighted in the list")],
    )
    geforce_only: BoolProperty(name="GeForce RTX only", default=True,
                               description="Off: also allow workstation / datacenter NVIDIA cards (A-series, L40, etc.)")
    gpu_name_contains: StringProperty(name="GPU name contains", default="", description="e.g. 4090, 3080 Ti, A5000")
    min_ram_gb: IntProperty(name="Min RAM (GB)", default=16, min=0, max=1024, description="Minimum system memory on the host")
    min_cpu_cores: IntProperty(name="Min CPU cores", default=4, min=0, max=256, description="Minimum effective CPU cores")
    min_dlperf: FloatProperty(name="Min DLPerf", default=0.0, min=0.0, soft_max=200.0, description="Vast's benchmark score (0 = any)")
    selected_offer_id: IntProperty(name="Selected Offer", default=0, options={"HIDDEN"})
    offers: CollectionProperty(type=CloudOfferItem)
    offer_index: IntProperty(default=-1, update=_offer_index_changed)

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
    min_gpus: IntProperty(name="Min GPUs", default=1, min=1, max=16, update=_min_gpus_changed,
                          description="Minimum GPUs per machine. Cycles renders each frame on all of a machine's GPUs")
    max_gpus: IntProperty(name="Max GPUs", default=1, min=1, max=16, update=_max_gpus_changed,
                          description="Maximum GPUs per machine (1 = single-GPU machines only)")
    multi_gpu_mode: EnumProperty(
        name="Multi-GPU", default="AUTO",
        items=[("AUTO", "Auto (fastest)", "Animations: one Blender per GPU, each on its own frames, as many as host RAM fits "
                                          "(GPUs are grouped if not all fit). Single images: all GPUs on the frame"),
               ("PER_GPU", "One Blender per GPU", "Always one process per GPU; every process holds its own copy of the scene in host RAM"),
               ("COMBINED", "All GPUs per frame", "One Blender renders each frame on all GPUs: least host RAM, but GPUs idle during scene sync")],
    )
    min_vram_gb: IntProperty(name="Min VRAM", default=8, min=4, max=48, subtype="UNSIGNED",
                             description="Minimum memory per GPU in GB (the scene must fit on every card; VRAM is not pooled)")
    max_price: FloatProperty(name="Max $/GPU/hour", default=0.60, min=0.0, soft_max=5.0, precision=3,
                             description="Maximum hourly price per GPU: a 4-GPU machine may cost up to 4x this (0 = no limit)")
    min_reliability: FloatProperty(name="Min Reliability", default=0.95, min=0.5, max=1.0, precision=2,
                                   description="Vast.ai host reliability score")
    disk_gb: IntProperty(name="Disk (GB)", default=40, min=10, max=500,
                         description="Container disk for Blender, the bundle and rendered frames")
    min_inet_down: IntProperty(name="Min Download Mbps", default=200, min=10, max=5000)
    min_inet_up: IntProperty(name="Min Upload Mbps", default=100, min=0, max=5000,
                             description="Host uplink speed; rendered frames are uploaded from the machine, so slow uplinks stall the job")
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
