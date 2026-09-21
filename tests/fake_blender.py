"""Stands in for blender: parses the worker's command line and mimics the render log."""
import os, sys, time

a = sys.argv[1:]
out = a[a.index("-o") + 1]
if "-a" in a:
    s, e, j = (int(a[a.index(k) + 1]) for k in ("-s", "-e", "-j"))
    frames = list(range(s, e + 1, j))
else:
    frames = [int(x) for x in a[a.index("-f") + 1].split(",")]
vis = os.environ.get("CUDA_VISIBLE_DEVICES", "all")
print(f"[gpu_setup] using OPTIX (visible GPUs: {vis})", flush=True)
print("[gpu_setup] DEVICE_USED=OPTIX", flush=True)
fail = os.environ.get("FAKE_FAIL_GPU", "")
for n, f in enumerate(frames):
    print(f"Fra:{f} Mem:100.00M (Peak 1.00G) | Time:00:00.10 | Synchronizing object | Cube", flush=True)
    time.sleep(0.15)
    print(f"Fra:{f} Mem:100.00M (Peak 1.00G) | Time:00:00.10 | Sample 1/8", flush=True)
    time.sleep(0.25)
    if fail and vis == fail and n == 1:
        print("Error: Out of memory in CUDA queue enqueue", flush=True)
        sys.exit(1)
    path = f"{out}{f:04d}.png"
    with open(path, "w") as fh:
        fh.write(vis)
    print(f"Saved: '{path}'", flush=True)
