"""Simulate render_worker.run_render on a pretend multi-GPU host (no network, no Blender).

    python tests/test_worker_multigpu.py

tests/fake_blender.py stands in for Blender; GPU count and RAM headroom are patched per scenario.
"""
import importlib.util, os, shutil, sys, types

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "_out", "simwork")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(os.path.join(WORK, "bundle"))
os.environ.update({"CR_WORK": WORK, "CR_JOB_ID": "sim", "CR_R2_ENDPOINT": "http://localhost:1", "CR_R2_BUCKET": "b",
                   "CR_R2_ACCESS_KEY": "k", "CR_R2_SECRET_KEY": "s"})
try:
    import boto3  # noqa: F401
except ImportError:
    b = types.ModuleType("boto3"); b.client = lambda *a, **k: None
    bc = types.ModuleType("botocore"); bcc = types.ModuleType("botocore.config"); bcc.Config = lambda **k: None
    sys.modules.update({"boto3": b, "botocore": bc, "botocore.config": bcc})
spec = importlib.util.spec_from_file_location("render_worker", os.path.join(os.path.dirname(HERE), "worker", "render_worker.py"))
rw = importlib.util.module_from_spec(spec); spec.loader.exec_module(rw)

real_cmd = rw.blender_cmd
CMDS = []
def fake_cmd(manifest, frames):
    cmd = real_cmd(manifest, frames); CMDS.append(cmd)
    return [sys.executable, os.path.join(HERE, "fake_blender.py")] + cmd[1:]
rw.blender_cmd = fake_cmd
rw.proc_rss = lambda pid: 4 * 1024 ** 3
rw.Status.push = lambda self: None
GB = 1024 ** 3


class FakeUploader:
    def __init__(self): self.items = []
    def enqueue(self, p): self.items.append(p)


def scenario(name, gpus, frames, mode, headroom_gb, fail_gpu="", expect_modes=None):
    shutil.rmtree(rw.OUT_DIR, ignore_errors=True); CMDS.clear()
    rw.GPU_MODE = mode
    rw.gpu_indices = lambda: [str(i) for i in range(gpus)]
    rw.ram_headroom = lambda: int(headroom_gb * GB)
    os.environ["FAKE_FAIL_GPU"] = fail_gpu
    st, up = rw.Status(), FakeUploader()
    rc = rw.run_render({"blend_file": "x.blend", "output_basename": "shot_"}, frames, st, up)
    files = sorted(p.name for p in rw.OUT_DIR.iterdir())
    want = [f"shot_{f:04d}.png" for f in frames]
    used = {}
    for p in rw.OUT_DIR.iterdir():
        used.setdefault(p.read_text(), []).append(int(p.stem.split("_")[1]))
    ok = rc == 0 and files == want and sorted(st.data["frames_done"]) == frames and len({str(p) for p in up.items}) == len(frames)
    if expect_modes:
        ok = ok and st.data["render_mode"] in expect_modes
    print(f"\n{'PASS' if ok else 'FAIL'} {name}: rc={rc} mode='{st.data['render_mode']}' frames/visible-GPUs={ {k: sorted(v) for k, v in used.items()} }")
    for c in CMDS:
        print("     cmd:", " ".join(c[c.index('-o') + 2:]))
    return ok


R = [
    scenario("A auto, RAM fits 4", 4, list(range(1, 21)), "AUTO", 64, expect_modes=["4 processes x 1/1/1/1 GPUs"]),
    scenario("B auto, RAM fits 2 -> 2 groups of 2", 4, list(range(1, 21)), "AUTO", 6, expect_modes=["2 processes x 2/2 GPUs"]),
    scenario("C auto, RAM fits 1 -> combined", 4, list(range(1, 11)), "AUTO", 1, expect_modes=["1 process x 4 GPUs (RAM-limited)"]),
    scenario("D per-gpu, gpu 2 dies -> fallback", 4, list(range(1, 21)), "PER_GPU", 64, fail_gpu="2", expect_modes=["1 process x all GPUs (fallback)"]),
    scenario("E combined", 4, list(range(1, 7)), "COMBINED", 64, expect_modes=["1 process x 4 GPUs"]),
    scenario("F frame step 2, 3 GPUs", 3, list(range(1, 30, 2)), "AUTO", 64),
    scenario("G single image on 8 GPUs", 8, [7], "AUTO", 64, expect_modes=["1 process x 8 GPUs"]),
    scenario("H 3 frames on 8 GPUs", 8, [1, 2, 3], "AUTO", 64, expect_modes=["3 processes x 3/3/2 GPUs"]),
    scenario("I retry list, 2 GPUs", 2, [3, 4, 9, 15, 16], "AUTO", 64),
    scenario("J single GPU", 1, list(range(1, 6)), "AUTO", 64, expect_modes=[""]),
]
print(f"\n{sum(R)}/{len(R)} scenarios passed")
sys.exit(0 if all(R) else 1)
