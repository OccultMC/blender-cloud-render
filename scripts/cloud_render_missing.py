"""Render the frames a cloud render job never delivered, from the bundle it already has in R2.

    python scripts/cloud_render_missing.py --job 20260922-030337-c5180e --output-dir D:/outputt --workers 8 \
        [--env path/.env] [--gpu-name 5090] [--max-dph 0.6] [--min-gpus 1 --max-gpus 2] [--dry-run]

Nothing is packed or uploaded: the job's bundle.zip + job.json must still be in R2 (a cancelled
or failed job keeps them). Missing = frames listed in the job's manifest that are not under
jobs/<id>/frames/. They are split over new workers, rendered, and downloaded into --output-dir;
frames already in that folder are not downloaded again. Ctrl+C cancels and destroys the machines.
"""
import argparse
import importlib
import os
import re
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pkg = types.ModuleType("cloud_render")
pkg.__path__ = [os.path.join(ROOT, "addon", "cloud_render")]
sys.modules["cloud_render"] = pkg
jobmod = importlib.import_module("cloud_render.job")
r2mod = importlib.import_module("cloud_render.r2")

ap = argparse.ArgumentParser()
ap.add_argument("--job", required=True, help="job id (the folder name under <prefix>/jobs/ in R2)")
ap.add_argument("--output-dir", required=True)
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--env", default=r"D:\GeoAxis\Hypervision\VPS_Scraper\.env")
ap.add_argument("--r2-prefix", default="blender-cloud-render")
ap.add_argument("--job-root", default=os.path.join(ROOT, "jobs"))
ap.add_argument("--max-dph", type=float, default=0.6, help="max $/hour per GPU (0 = no limit)")
ap.add_argument("--min-gpus", type=int, default=1)
ap.add_argument("--max-gpus", type=int, default=1)
ap.add_argument("--gpu-mode", default="AUTO", choices=["AUTO", "PER_GPU", "COMBINED"])
ap.add_argument("--pick", default="CHEAPEST_GPU", choices=["CHEAPEST", "CHEAPEST_GPU", "BEST", "FASTEST"])
ap.add_argument("--gpu-name", default="", help="GPU name must contain this, e.g. 5090")
ap.add_argument("--all-nvidia", action="store_true", help="also allow workstation / datacenter cards")
ap.add_argument("--series", default="20,30,40,50")
ap.add_argument("--min-vram", type=int, default=8)
ap.add_argument("--min-ram", type=int, default=32)
ap.add_argument("--disk", type=int, default=40)
ap.add_argument("--min-reliability", type=float, default=0.95)
ap.add_argument("--min-inet-down", type=int, default=200)
ap.add_argument("--min-inet-up", type=int, default=200)
ap.add_argument("--retries", type=int, default=2)
ap.add_argument("--image", default="ghcr.io/occultmc/blender-cloud-render:latest")
ap.add_argument("--dry-run", action="store_true", help="list the missing frames and the plan, rent nothing")
args = ap.parse_args()

env = {}
with open(args.env, encoding="utf-8") as fh:
    for line in fh:
        if "=" in line and not line.startswith("#"):
            k, _, v = line.strip().partition("=")
            env[k] = v.strip().strip('"')
vast_key = env.get("VAST_API_KEY") or open(os.path.expanduser("~/.config/vastai/vast_api_key")).read().strip()

prefix = f"{args.r2_prefix.strip('/')}/jobs/{args.job}"
r2 = r2mod.R2Client(env["R2_ACCOUNT_ID"], env["R2_ACCESS_KEY_ID"], env["R2_SECRET_ACCESS_KEY"], env["R2_BUCKET_NAME"], "")
remote = r2.get_json(f"{prefix}/job.json")
if not remote or not r2.exists(f"{prefix}/bundle.zip"):
    sys.exit(f"no job.json / bundle.zip under {prefix}")
names = remote["manifest"]["frame_filenames"]
in_r2 = {o["key"][len(prefix) + len("/frames/"):] for o in r2.list(f"{prefix}/frames/")}
missing = sorted(int(f) for f, n in names.items() if n not in in_r2)
if not missing:
    sys.exit(f"{args.job}: all {len(names)} frames are in R2 - nothing to render")


def ranges(frames):
    out, start = [], frames[0]
    for a, b in zip(frames, frames[1:] + [None]):
        if b != a + 1:
            out.append(f"{start}-{a}" if a != start else str(a))
            start = b
    return ", ".join(out)


# even, contiguous chunks of the missing list; indices continue after the original workers so
# their status/log objects in R2 are left alone
n = max(1, min(args.workers, len(missing)))
size, extra = divmod(len(missing), n)
first_index = max([int(p["index"]) for p in remote.get("plan", [])] + [-1]) + 1
while r2.exists(f"{prefix}/status/worker_{first_index}.json"):
    first_index += n
plan, pos = [], 0
for i in range(n):
    part = missing[pos:pos + size + (1 if i < extra else 0)]
    pos += len(part)
    plan.append({"index": first_index + i, "frame_start": part[0], "frame_end": part[-1], "frame_step": 1, "frames": part})
print(f"[{args.job}] {remote.get('blend')}: {len(in_r2)}/{len(names)} frames in R2, {len(missing)} missing: {ranges(missing)}", flush=True)
for p in plan:
    print(f"   worker {p['index']}: {len(p['frames'])} frames ({ranges(p['frames'])})", flush=True)
if args.dry_run:
    sys.exit(0)

ver = tuple(int(x) for x in re.findall(r"\d+", str(remote.get("blender_version") or "5.2.1"))[:3])
out_dir = os.path.abspath(args.output_dir)
os.makedirs(out_dir, exist_ok=True)
cfg = jobmod.JobConfig(
    job_id=args.job, blend_path="", blend_name=str(remote.get("blend") or "scene"), blend_dir="",
    job_dir=os.path.join(args.job_root, f"{args.job}-missing-{time.strftime('%H%M%S')}"), output_dir=out_dir,
    tmp_blend="", blender_exe="", blender_version=ver, blender_version_string=str(remote.get("blender_version") or ""),
    plan=plan, frame_step=1,
    image=args.image, ghcr_user=env.get("GHCR_USER", "occultmc"), ghcr_token=env.get("GHCR_PAT", ""), pin_digest=True,
    vast_key=vast_key, r2_account=env["R2_ACCOUNT_ID"], r2_access_key=env["R2_ACCESS_KEY_ID"],
    r2_secret_key=env["R2_SECRET_ACCESS_KEY"], r2_bucket=env["R2_BUCKET_NAME"], r2_endpoint="",
    r2_prefix=args.r2_prefix, series=[s.strip() for s in args.series.split(",") if s.strip()],
    min_vram_gb=args.min_vram, disk_gb=args.disk, max_dph=args.max_dph, min_reliability=args.min_reliability,
    min_inet_down=args.min_inet_down, auto_download=True, auto_destroy=True, max_retries=args.retries,
    keep_bundle_in_r2=True, pick_strategy=args.pick, min_cpu_ram_gb=args.min_ram, gpu_name_contains=args.gpu_name,
    geforce_only=not args.all_nvidia, min_gpus=args.min_gpus, max_gpus=args.max_gpus, multi_gpu_mode=args.gpu_mode,
    min_inet_up=args.min_inet_up, reuse_bundle=True,
)
job = jobmod.CloudJob(cfg)
# frames that are already in the output folder must not be downloaded again
job.downloaded = {name for name in in_r2 if os.path.exists(os.path.join(out_dir, name))}
want = {names[str(f)] for f in missing}
job.start()
last = ""
try:
    while job.thread.is_alive():
        snap = job.snapshot()
        got = len(want & job.frames_in_r2)
        active = sum(1 for w in snap["workers"] if w["state"] not in ("done", "dead", "failed", "pending"))
        line = f"{snap['status']} {got}/{len(want)} missing frames rendered, {active} active, ${snap['cost_per_hour']:.2f}/h"
        if line != last:
            print(time.strftime("%H:%M:%S"), line, flush=True)
            last = line
        time.sleep(10)
except KeyboardInterrupt:
    print("cancelling - destroying instances", flush=True)
    job.cancel()
    job.thread.join(300)
snap = job.snapshot()
local = [n_ for n_ in want if os.path.exists(os.path.join(out_dir, n_))]
print(f"FINAL {snap['status']}: {snap['message']} | est. cost ${snap['cost_so_far']:.3f} | {len(local)}/{len(want)} of the missing frames now in {out_dir}", flush=True)
for w in snap["workers"]:
    if w["state"] != "done":
        print(f"   worker {w['index']} {w['state']} frames {w['frame_start']}-{w['frame_end']} err={w['error'][:120]}", flush=True)
sys.exit(0 if snap["status"] == "done" and len(local) == len(want) else 1)
