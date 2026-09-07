"""Launch a cloud render job from the command line (no Blender UI needed).

    python scripts/cloud_render_cli.py --blend "C:/path/scene.blend" --workers 10 --output-dir "C:/out/scene" \
        [--samples 512] [--basename scene_] [--env path/.env] [--max-dph 0.6] [--min-vram 8]

Applies optional overrides (samples, output basename) to a temporary copy of the
.blend - the original file is never modified - then runs the same CloudJob the
add-on uses and streams progress until the job finishes.
"""
import argparse
import importlib
import json
import os
import subprocess
import sys
import time
import types
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pkg = types.ModuleType("cloud_render")
pkg.__path__ = [os.path.join(ROOT, "addon", "cloud_render")]
sys.modules["cloud_render"] = pkg
jobmod = importlib.import_module("cloud_render.job")
planner = importlib.import_module("cloud_render.planner")
prefs = importlib.import_module("cloud_render.prefs") if False else None  # prefs needs bpy; parse .env directly

DEFAULT_BLENDER = r"D:\Program Files\Blender Foundation\Blender 5.2\blender.exe"

ap = argparse.ArgumentParser()
ap.add_argument("--blend", required=True)
ap.add_argument("--workers", type=int, required=True)
ap.add_argument("--output-dir", required=True)
ap.add_argument("--env", default=r"D:\GeoAxis\Hypervision\VPS_Scraper\.env")
ap.add_argument("--blender", default=DEFAULT_BLENDER)
ap.add_argument("--samples", type=int, default=0, help="override Cycles samples (0 = keep file setting)")
ap.add_argument("--basename", default="", help="output file name prefix (default: <blend name>_)")
ap.add_argument("--job-root", default=os.path.join(ROOT, "jobs"))
ap.add_argument("--max-dph", type=float, default=0.6)
ap.add_argument("--min-vram", type=int, default=8)
ap.add_argument("--disk", type=int, default=40)
ap.add_argument("--series", default="20,30,40,50")
ap.add_argument("--min-reliability", type=float, default=0.95)
ap.add_argument("--min-inet-down", type=int, default=200)
ap.add_argument("--retries", type=int, default=2)
ap.add_argument("--image", default="ghcr.io/occultmc/blender-cloud-render:latest")
args = ap.parse_args()

env = {}
with open(args.env, encoding="utf-8") as fh:
    for line in fh:
        if "=" in line and not line.startswith("#"):
            k, _, v = line.strip().partition("=")
            env[k] = v.strip().strip('"')
vast_key = env.get("VAST_API_KEY") or open(os.path.expanduser("~/.config/vastai/vast_api_key")).read().strip()

blend = os.path.abspath(args.blend)
name = os.path.splitext(os.path.basename(blend))[0]
basename = args.basename or (name + "_")
job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
job_dir = os.path.join(args.job_root, job_id)
os.makedirs(job_dir, exist_ok=True)
tmp_blend = os.path.join(os.path.dirname(blend), f"{name}.cloudrender_tmp.blend")

# 1. temp copy with overrides + read frame range (background Blender, original untouched)
expr = f"""
import bpy, json
s = bpy.context.scene
if {args.samples} > 0:
    s.cycles.samples = {args.samples}
s.render.filepath = '//' + {basename!r}
bpy.ops.wm.save_as_mainfile(filepath={tmp_blend!r}, copy=True, compress=False, relative_remap=True)
print('CLIINFO ' + json.dumps({{'frame_start': s.frame_start, 'frame_end': s.frame_end, 'frame_step': s.frame_step,
    'samples': s.cycles.samples, 'engine': s.render.engine, 'fmt': s.render.image_settings.file_format,
    'res': [s.render.resolution_x, s.render.resolution_y, s.render.resolution_percentage],
    'denoise': s.cycles.use_denoising, 'version': bpy.app.version[:3]}}))
"""
out = subprocess.run([args.blender, "-b", blend, "--python-exit-code", "1", "--python-expr", expr],
                     capture_output=True, text=True, encoding="utf-8", errors="replace")
info = None
for line in out.stdout.splitlines():
    if line.startswith("CLIINFO "):
        info = json.loads(line[8:])
if out.returncode != 0 or info is None:
    print(out.stdout[-3000:], out.stderr[-2000:])
    sys.exit("could not prepare the blend copy")
if info["engine"] != "CYCLES":
    sys.exit(f"{name}: engine is {info['engine']}, cloud render needs Cycles")
if info["fmt"] == "FFMPEG":
    sys.exit(f"{name}: output format is a video; switch to an image format")
plan = planner.split_frames(info["frame_start"], info["frame_end"], info["frame_step"], args.workers)
print(f"[{name}] frames {info['frame_start']}-{info['frame_end']} step {info['frame_step']} samples={info['samples']} "
      f"{info['res'][0]}x{info['res'][1]}@{info['res'][2]}% {info['fmt']} denoise={info['denoise']} -> "
      f"{planner.describe_plan(plan)}; output {args.output_dir}\\{basename}####", flush=True)

cfg = jobmod.JobConfig(
    job_id=job_id, blend_path=blend, blend_name=name, blend_dir=os.path.dirname(blend), job_dir=job_dir,
    output_dir=os.path.abspath(args.output_dir), tmp_blend=tmp_blend, blender_exe=args.blender,
    blender_version=tuple(info["version"]), blender_version_string=".".join(map(str, info["version"])),
    plan=plan, frame_step=max(1, info["frame_step"]),
    image=args.image, ghcr_user=env.get("GHCR_USER", "occultmc"), ghcr_token=env.get("GHCR_PAT", ""), pin_digest=True,
    vast_key=vast_key, r2_account=env["R2_ACCOUNT_ID"], r2_access_key=env["R2_ACCESS_KEY_ID"],
    r2_secret_key=env["R2_SECRET_ACCESS_KEY"], r2_bucket=env["R2_BUCKET_NAME"], r2_endpoint="",
    r2_prefix="blender-cloud-render", series=[s.strip() for s in args.series.split(",") if s.strip()],
    min_vram_gb=args.min_vram, disk_gb=args.disk, max_dph=args.max_dph, min_reliability=args.min_reliability,
    min_inet_down=args.min_inet_down, auto_download=True, auto_destroy=True, max_retries=args.retries,
)
job = jobmod.CloudJob(cfg)
job.start()
last = ""
while job.thread.is_alive():
    snap = job.snapshot()
    active = sum(1 for w in snap["workers"] if w["state"] not in ("done", "dead", "failed", "pending"))
    line = f"[{name}] {snap['status']} {snap['frames_done']}/{snap['frames_total']} frames, {active} active, ${snap['cost_per_hour']:.2f}/h"
    if line != last:
        print(time.strftime("%H:%M:%S"), line, flush=True)
        last = line
    time.sleep(10)
snap = job.snapshot()
print(f"[{name}] FINAL {snap['status']}: {snap['message']} | est. cost ${snap['cost_so_far']:.3f} | job {job_id}", flush=True)
if snap["error"]:
    print(f"[{name}] error: {snap['error']}", flush=True)
for w in snap["workers"]:
    if w["state"] != "done":
        print(f"[{name}]   worker {w['index']} {w['state']} frames {w['frame_start']}-{w['frame_end']} err={w['error'][:100]}", flush=True)
sys.exit(0 if snap["status"] == "done" else 1)
