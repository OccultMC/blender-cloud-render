"""Orchestrator dry run without renting anything.

    python tests/test_job_dry.py --env path/to/.env --blender "D:/Program Files/.../blender.exe"

Runs CloudJob pack + upload stages for real (R2), then fakes worker frames and
checks polling / download / persistence.  Vast is only used read-only.
"""
import argparse
import importlib
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Import the package without executing __init__.py (which needs bpy).
import types  # noqa: E402
pkg = types.ModuleType("cloud_render")
pkg.__path__ = [os.path.join(ROOT, "addon", "cloud_render")]
sys.modules["cloud_render"] = pkg
jobmod = importlib.import_module("cloud_render.job")
planner = importlib.import_module("cloud_render.planner")

ap = argparse.ArgumentParser()
ap.add_argument("--env", required=True)
ap.add_argument("--blender", required=True)
args = ap.parse_args()

env = {}
for line in open(args.env, encoding="utf-8"):
    if "=" in line and not line.startswith("#"):
        k, _, v = line.strip().partition("=")
        env[k] = v.strip().strip('"')

OUT = os.path.join(ROOT, "tests", "_out")
blend = os.path.join(OUT, "scene_test.blend")
assert os.path.exists(blend), "run tests/test_addon.py first (creates tests/_out/scene_test.blend)"
FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


job_id = "dry-" + time.strftime("%H%M%S")
job_dir = os.path.join(OUT, "jobs", job_id)
tmp_blend = os.path.join(OUT, "scene_test.dry_tmp.blend")
shutil.copy2(blend, tmp_blend)
cfg = jobmod.JobConfig(
    job_id=job_id, blend_path=blend, blend_name="scene_test", blend_dir=OUT, job_dir=job_dir,
    output_dir=os.path.join(OUT, "frames_dl"), tmp_blend=tmp_blend, blender_exe=args.blender,
    blender_version=(5, 2, 1), blender_version_string="5.2.1", plan=planner.split_frames(1, 12, 1, 3), frame_step=1,
    image="ghcr.io/occultmc/blender-cloud-render:latest", ghcr_user="occultmc", ghcr_token="", pin_digest=True,
    vast_key=open(os.path.expanduser("~/.config/vastai/vast_api_key")).read().strip() if not env.get("VAST_API_KEY") else env["VAST_API_KEY"],
    r2_account=env["R2_ACCOUNT_ID"], r2_access_key=env["R2_ACCESS_KEY_ID"], r2_secret_key=env["R2_SECRET_ACCESS_KEY"],
    r2_bucket=env["R2_BUCKET_NAME"], r2_endpoint="", r2_prefix="blender-cloud-render",
    series=["20", "30", "40", "50"], min_vram_gb=8, disk_gb=40, max_dph=0.6, min_reliability=0.95, min_inet_down=200,
    poll_interval=1.0,
)
job = jobmod.CloudJob(cfg)
try:
    job._stage_pack()
    check(job.manifest.get("blend_file") == "scene_test.blend" and len(job.frame_filenames) == 12, "pack stage produced manifest with 12 frame names")
    job._stage_upload()
    check(job.r2.exists(f"{job.prefix}/bundle.zip") and job.r2.get_json(f"{job.prefix}/job.json")["job_id"] == job_id, "bundle.zip + job.json in R2")
    check(not os.path.exists(os.path.join(job_dir, "bundle.zip")), "local zip removed after upload")

    # pin digest resolution (read-only GHCR call) - falls back to the tag if the package is not public yet
    from cloud_render.vast import resolve_image_digest
    pinned = resolve_image_digest(cfg.image)
    print("  image ->", pinned)

    # fake two workers finishing their frames
    for w in job.workers[:2]:
        for f in w.frames:
            job.r2.put_bytes(f"{job.prefix}/frames/{job.frame_filenames[str(f)]}", b"\x89PNGfake", "image/png")
    job._poll_frames()
    check(len(job.frames_in_r2) == sum(len(w.frames) for w in job.workers[:2]), f"poll sees {len(job.frames_in_r2)} frames")
    check(job.workers[0].state == "done" and job.workers[1].state == "done" and job.workers[2].state == "pending", "workers 0/1 done, 2 pending")
    job._download_new_frames()
    dl = sorted(os.listdir(cfg.output_dir))
    check(len(dl) == len(job.frames_in_r2) and dl[0] == "shot_0001.png", f"downloaded {len(dl)} frames to output dir ({dl[0]}..{dl[-1]})")
    snap = job.snapshot()
    check(abs(snap["progress"] - len(dl) / 12) < 1e-6 and snap["frames_total"] == 12, f"snapshot progress {snap['progress']:.2f}")

    # persistence round-trip
    job.save()
    loaded = jobmod.CloudJob.load(job_dir, {"vast_key": cfg.vast_key, "r2_access_key": cfg.r2_access_key,
                                            "r2_secret_key": cfg.r2_secret_key, "ghcr_token": ""})
    check(loaded.cfg.job_id == job_id and len(loaded.workers) == 3 and loaded.workers[0].state == "done"
          and loaded.frame_filenames == job.frame_filenames and loaded.downloaded == job.downloaded, "job.json round-trip restores workers/frames")
    saved = json.load(open(os.path.join(job_dir, "job.json")))
    check(saved["config"]["vast_key"] == "" and saved["config"]["r2_secret_key"] == "", "secrets not written to job.json")

    # nothing to destroy: must not raise
    job._destroy_all()
    check(True, "destroy_all with no instances is a no-op")

    # teardown path removes bundle from R2 (keep_bundle False) - emulate finish without instances
    job._finish()
    check(job.status == "done" and not job.r2.exists(f"{job.prefix}/bundle.zip"), f"finish -> status {job.status}, bundle removed")
finally:
    n = job.r2.delete_prefix(job.prefix + "/")
    print(f"  cleanup: removed {n} objects under {job.prefix}")
    job._cleanup_local()

print("\n==== %d failure(s) ====" % len(FAILS))
for f in FAILS:
    print(" - " + f)
sys.exit(1 if FAILS else 0)
