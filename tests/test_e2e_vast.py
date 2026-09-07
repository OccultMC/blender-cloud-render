"""Real end-to-end run: rents ONE cheap RTX worker on Vast.ai and renders a few tiny frames.

    python tests/test_e2e_vast.py --env path/to/.env --blender "...blender.exe" [--ghcr-token TOKEN] [--max-dph 0.15]

Costs a few cents.  Verifies: offer search, instance creation with our image,
OptiX inside the container, frame upload, download, self-destroy + cleanup.
"""
import argparse
import importlib
import os
import shutil
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pkg = types.ModuleType("cloud_render")
pkg.__path__ = [os.path.join(ROOT, "addon", "cloud_render")]
sys.modules["cloud_render"] = pkg
jobmod = importlib.import_module("cloud_render.job")
planner = importlib.import_module("cloud_render.planner")
vastmod = importlib.import_module("cloud_render.vast")

ap = argparse.ArgumentParser()
ap.add_argument("--env", required=True)
ap.add_argument("--blender", required=True)
ap.add_argument("--ghcr-token", default="")
ap.add_argument("--max-dph", type=float, default=0.15)
ap.add_argument("--frames", type=int, default=4)
ap.add_argument("--timeout-min", type=float, default=25)
args = ap.parse_args()

env = {}
for line in open(args.env, encoding="utf-8"):
    if "=" in line and not line.startswith("#"):
        k, _, v = line.strip().partition("=")
        env[k] = v.strip().strip('"')
vast_key = env.get("VAST_API_KEY") or open(os.path.expanduser("~/.config/vastai/vast_api_key")).read().strip()

OUT = os.path.join(ROOT, "tests", "_out")
blend = os.path.join(OUT, "scene_test.blend")
assert os.path.exists(blend), "run tests/test_addon.py first"

job_id = "e2e-" + time.strftime("%Y%m%d-%H%M%S")
job_dir = os.path.join(OUT, "jobs", job_id)
tmp_blend = os.path.join(OUT, "scene_test.e2e_tmp.blend")
shutil.copy2(blend, tmp_blend)
out_dir = os.path.join(OUT, "e2e_frames")
shutil.rmtree(out_dir, ignore_errors=True)

cfg = jobmod.JobConfig(
    job_id=job_id, blend_path=blend, blend_name="scene_test", blend_dir=OUT, job_dir=job_dir,
    output_dir=out_dir, tmp_blend=tmp_blend, blender_exe=args.blender,
    blender_version=(5, 2, 1), blender_version_string="5.2.1 LTS",
    plan=planner.split_frames(1, args.frames, 1, 1), frame_step=1,
    image="ghcr.io/occultmc/blender-cloud-render:latest", ghcr_user="occultmc", ghcr_token=args.ghcr_token, pin_digest=True,
    vast_key=vast_key, r2_account=env["R2_ACCOUNT_ID"], r2_access_key=env["R2_ACCESS_KEY_ID"],
    r2_secret_key=env["R2_SECRET_ACCESS_KEY"], r2_bucket=env["R2_BUCKET_NAME"], r2_endpoint="", r2_prefix="blender-cloud-render",
    series=["20", "30", "40", "50"], min_vram_gb=8, disk_gb=30, max_dph=args.max_dph, min_reliability=0.95, min_inet_down=200,
    auto_download=True, auto_destroy=True, max_retries=1, poll_interval=10.0, stale_minutes=10, loading_timeout_minutes=12,
)
job = jobmod.CloudJob(cfg)
job.start()
deadline = time.time() + args.timeout_min * 60
last = ""
while job.thread.is_alive() and time.time() < deadline:
    snap = job.snapshot()
    line = f"{snap['status']} | {snap['message']} | {snap['frames_done']}/{snap['frames_total']} | " + \
           "; ".join(f"w{w['index']}:{w['state']}:{w.get('current_frame')}" for w in snap["workers"])
    if line != last:
        print(time.strftime("%H:%M:%S"), line, flush=True)
        last = line
    time.sleep(5)
if job.thread.is_alive():
    print("TIMEOUT - cancelling", flush=True)
    job.cancel()
    job.thread.join(180)

snap = job.snapshot()
print("\nFINAL:", snap["status"], "-", snap["message"])
print("error:", snap["error"])
print("cost/h:", snap["cost_per_hour"], "est cost:", round(snap["cost_so_far"], 4))
for w in snap["workers"]:
    print(f"  worker {w['index']}: state={w['state']} instance={w['instance_id']} gpu={w['offer'].get('gpu_name')} "
          f"${w['offer'].get('dph_total')}/h device={w['device_used']} frames_done={w['frames_done']} err={w['error'][:120]}")
frames = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
print("downloaded frames:", frames)

# leftover check: nothing labelled with this job may still be alive
client = vastmod.VastClient(vast_key)
alive = [i for i in client.list_instances() if str(i.get("label") or "").startswith(f"cloudrender-{job_id[:8]}")]
print("instances still alive for this job:", [i["id"] for i in alive])
if alive:
    print("force destroying...", client.destroy_many([int(i["id"]) for i in alive]))
print("log tail:")
for l in job.log_lines[-25:]:
    print("  " + l)
ok = snap["status"] == "done" and len(frames) == args.frames and not alive
print("\nE2E", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
