"""Bundle building: snapshot the session, pack it in a background Blender, zip it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from typing import Callable

PACK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pack_blend.py")


def snapshot_session(blend_path: str) -> str:
    """Main-thread only.  Save an exact copy of the *current* session next to the
    original .blend (same directory so relative paths stay valid) and return it."""
    import bpy

    base, _ = os.path.splitext(os.path.basename(blend_path))
    tmp = os.path.join(os.path.dirname(blend_path), f"{base}.cloudrender_tmp.blend")
    bpy.ops.wm.save_as_mainfile(filepath=tmp, copy=True, compress=False, relative_remap=True)
    return tmp


def run_pack(blender_exe: str, tmp_blend: str, out_dir: str, name: str, orig_dir: str,
             log: Callable[[str], None]) -> tuple[dict, dict]:
    """Run pack_blend.py in a background Blender.  Returns (manifest, report)."""
    cmd = [
        blender_exe, "-b", tmp_blend, "--factory-startup", "--python-exit-code", "3",
        "--python", PACK_SCRIPT, "--", "--out", out_dir, "--name", name, "--orig-dir", orig_dir,
    ]
    log("packing: " + " ".join(f'"{c}"' if " " in c else c for c in cmd[:3]) + " ...")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", creationflags=creationflags)
    tail = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        tail.append(line)
        if len(tail) > 60:
            tail.pop(0)
        if line.startswith("[pack_blend]"):
            log(line)
    rc = proc.wait()
    manifest_path = os.path.join(out_dir, "manifest.json")
    report_path = os.path.join(out_dir, "pack_report.json")
    if rc != 0 or not os.path.exists(manifest_path):
        raise RuntimeError(f"packing failed (exit {rc}):\n" + "\n".join(tail[-15:]))
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    report = {}
    if os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    return manifest, report


def zip_bundle(bundle_dir: str, zip_path: str, log: Callable[[str], None]) -> int:
    total = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for root, _dirs, files in os.walk(bundle_dir):
            for f in files:
                if f.endswith(".blend1"):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, bundle_dir).replace("\\", "/")
                zf.write(full, rel)
                total += os.path.getsize(full)
    log(f"bundle zipped: {total / 1e6:.1f} MB -> {os.path.basename(zip_path)}")
    return total
