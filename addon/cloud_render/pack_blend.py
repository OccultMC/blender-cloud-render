"""Runs in a *background* Blender process to build a self-contained render bundle.

    blender -b <copy-of-scene.blend> --python pack_blend.py -- --out <dir> --name <blend_name> --orig-dir <dir>

Steps
  1. save the file into the bundle directory (relative paths are remapped so
     everything still resolves)
  2. pack linked libraries and every packable external file (images, UDIM
     tiles, sounds, fonts, volumes) straight into the .blend
  3. copy everything Blender cannot pack (image sequences, movies, movie clips,
     Alembic/USD caches, fluid/ocean/mesh caches, sequencer strips, disk point
     caches) into <bundle>/_ext and point the data-blocks at the copies
  4. make all paths relative and save again
  5. write manifest.json (scene settings the worker needs) and pack_report.json

The artist's own session is never modified: this operates on a temporary copy.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import sys
import traceback

import bpy

REPORT = {"packed": [], "copied": [], "warnings": [], "missing": [], "errors": []}


def _args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    out = {"out": "", "name": "", "orig_dir": ""}
    i = 0
    while i < len(argv):
        if argv[i] == "--out":
            out["out"] = argv[i + 1]; i += 2
        elif argv[i] == "--name":
            out["name"] = argv[i + 1]; i += 2
        elif argv[i] == "--orig-dir":
            out["orig_dir"] = argv[i + 1]; i += 2
        else:
            i += 1
    return out


ARGS = _args()
OUT_DIR = os.path.abspath(ARGS["out"])
NAME = ARGS["name"] or "scene"
ORIG_DIR = ARGS["orig_dir"]
EXT_DIR = os.path.join(OUT_DIR, "_ext")


def log(msg):
    print(f"[pack_blend] {msg}", flush=True)


def warn(msg):
    REPORT["warnings"].append(msg)
    log("WARNING: " + msg)


def absp(p: str) -> str:
    return os.path.normpath(bpy.path.abspath(p)) if p else ""


def _hash(p: str) -> str:
    return hashlib.sha1(p.encode("utf-8", "replace")).hexdigest()[:8]


def copy_file(src_abs: str, subdir: str = "") -> str | None:
    """Copy one file into _ext/ and return its absolute destination (None if missing)."""
    if not src_abs or not os.path.isfile(src_abs):
        REPORT["missing"].append(src_abs)
        return None
    dest_dir = os.path.join(EXT_DIR, subdir) if subdir else EXT_DIR
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{_hash(src_abs)}_{os.path.basename(src_abs)}")
    if not os.path.exists(dest):
        shutil.copy2(src_abs, dest)
        REPORT["copied"].append(src_abs)
    return dest


def copy_dir(src_abs: str, tag: str) -> str | None:
    if not src_abs or not os.path.isdir(src_abs):
        REPORT["missing"].append(src_abs)
        return None
    dest = os.path.join(EXT_DIR, f"{tag}_{_hash(src_abs)}")
    if not os.path.exists(dest):
        shutil.copytree(src_abs, dest)
        REPORT["copied"].append(src_abs + os.sep)
    return dest


def copy_sequence(first_abs: str, tag: str) -> str | None:
    """Copy all files of a numbered sequence that share the first frame's prefix/suffix."""
    if not first_abs:
        return None
    d, base = os.path.split(first_abs)
    m = re.match(r"^(.*?)(\d+)(\D*)$", base)
    if not m or not os.path.isdir(d):
        REPORT["missing"].append(first_abs)
        return None
    prefix, digits, suffix = m.groups()
    pattern = re.compile(re.escape(prefix) + r"\d+" + re.escape(suffix) + "$")
    files = [f for f in os.listdir(d) if pattern.match(f)]
    if not files:
        REPORT["missing"].append(first_abs)
        return None
    dest_dir = os.path.join(EXT_DIR, f"{tag}_{_hash(d + prefix)}")
    os.makedirs(dest_dir, exist_ok=True)
    for f in files:
        dst = os.path.join(dest_dir, f)
        if not os.path.exists(dst):
            shutil.copy2(os.path.join(d, f), dst)
    REPORT["copied"].append(f"{first_abs} (+{len(files) - 1} frames)")
    return os.path.join(dest_dir, base)


def is_packed(idblock) -> bool:
    if getattr(idblock, "packed_file", None):
        return True
    pf = getattr(idblock, "packed_files", None)
    return bool(pf and len(pf))


# --------------------------------------------------------------------------- #
# passes
# --------------------------------------------------------------------------- #

def pass_libraries():
    if not bpy.data.libraries:
        return
    try:
        bpy.ops.file.pack_libraries()
        REPORT["packed"].extend(f"library {lib.filepath}" for lib in bpy.data.libraries)
    except Exception as exc:
        warn(f"pack_libraries failed: {exc}")
    for lib in bpy.data.libraries:
        if not is_packed(lib):
            dst = copy_file(absp(lib.filepath), "libs")
            if dst:
                lib.filepath = dst


def pass_pack_all():
    try:
        bpy.ops.file.pack_all()
    except Exception as exc:
        warn(f"pack_all reported: {exc}")


def pass_images():
    for img in bpy.data.images:
        if img.library or is_packed(img):
            continue
        src = img.source
        if src in {"GENERATED", "VIEWER"} or not img.filepath:
            continue
        if src in {"FILE", "TILED"}:
            try:
                img.pack()
                REPORT["packed"].append(f"image {img.name}")
                continue
            except Exception as exc:
                warn(f"image '{img.name}' could not be packed ({exc}); copying instead")
            if src == "TILED":
                # <UDIM> pattern: copy every tile.
                first = absp(img.filepath)
                d, base = os.path.split(first)
                if "<UDIM>" in base:
                    dest_dir = os.path.join(EXT_DIR, f"udim_{_hash(first)}")
                    os.makedirs(dest_dir, exist_ok=True)
                    n = 0
                    for f in glob.glob(os.path.join(d, base.replace("<UDIM>", "[0-9][0-9][0-9][0-9]"))):
                        shutil.copy2(f, os.path.join(dest_dir, os.path.basename(f)))
                        n += 1
                    if n:
                        img.filepath = os.path.join(dest_dir, base)
                        REPORT["copied"].append(f"{first} ({n} UDIM tiles)")
                    else:
                        REPORT["missing"].append(first)
                    continue
            dst = copy_file(absp(img.filepath))
            if dst:
                img.filepath = dst
        elif src == "SEQUENCE":
            dst = copy_sequence(absp(img.filepath), "seq")
            if dst:
                img.filepath = dst
        elif src == "MOVIE":
            dst = copy_file(absp(img.filepath), "movies")
            if dst:
                img.filepath = dst


def pass_movieclips():
    for clip in bpy.data.movieclips:
        if clip.library or not clip.filepath:
            continue
        if clip.source == "SEQUENCE":
            dst = copy_sequence(absp(clip.filepath), "clip")
        else:
            dst = copy_file(absp(clip.filepath), "clips")
        if dst:
            clip.filepath = dst


def pass_sounds_fonts_volumes():
    for snd in bpy.data.sounds:
        if snd.library or is_packed(snd) or not snd.filepath:
            continue
        try:
            snd.pack()
            REPORT["packed"].append(f"sound {snd.name}")
        except Exception:
            dst = copy_file(absp(snd.filepath), "sounds")
            if dst:
                snd.filepath = dst
    for font in bpy.data.fonts:
        fp = font.filepath
        if font.library or is_packed(font) or not fp or fp == "<builtin>":
            continue
        try:
            font.pack()
            REPORT["packed"].append(f"font {font.name}")
        except Exception:
            dst = copy_file(absp(fp), "fonts")
            if dst:
                font.filepath = dst
    for vol in getattr(bpy.data, "volumes", []):
        if vol.library or is_packed(vol) or not vol.filepath:
            continue
        if getattr(vol, "is_sequence", False):
            dst = copy_sequence(absp(vol.filepath), "vdb")
        else:
            try:
                vol.pack()
                REPORT["packed"].append(f"volume {vol.name}")
                continue
            except Exception:
                dst = copy_file(absp(vol.filepath), "vdb")
        if dst:
            vol.filepath = dst


def pass_cache_files():
    for cf in getattr(bpy.data, "cache_files", []):
        if cf.library or not cf.filepath:
            continue
        dst = copy_file(absp(cf.filepath), "caches")
        if dst:
            cf.filepath = dst


def pass_modifiers():
    for ob in bpy.data.objects:
        if ob.library:
            continue
        for md in ob.modifiers:
            try:
                if md.type == "FLUID" and md.fluid_type == "DOMAIN":
                    ds = md.domain_settings
                    dst = copy_dir(absp(ds.cache_directory), "fluid")
                    if dst:
                        ds.cache_directory = dst
                elif md.type == "OCEAN" and getattr(md, "is_cached", False):
                    dst = copy_dir(absp(md.filepath), "ocean")
                    if dst:
                        md.filepath = dst
                elif md.type == "MESH_CACHE" and md.filepath:
                    dst = copy_file(absp(md.filepath), "meshcache")
                    if dst:
                        md.filepath = dst
                elif md.type == "DYNAMIC_PAINT" and md.canvas_settings:
                    for surf in md.canvas_settings.canvas_surfaces:
                        if surf.surface_format == "IMAGE_SEQUENCE" and surf.image_output_path:
                            dst = copy_dir(absp(surf.image_output_path), "dpaint")
                            if dst:
                                surf.image_output_path = dst
            except Exception as exc:
                warn(f"modifier '{md.name}' on '{ob.name}': {exc}")
        for ps in ob.particle_systems:
            pc = ps.point_cache
            if pc.use_external and pc.filepath:
                dst = copy_dir(absp(pc.filepath), "pcache")
                if dst:
                    pc.filepath = dst


def pass_sequencer():
    def walk(strips):
        for s in strips:
            yield s
            if s.type == "META":
                yield from walk(s.sequences if hasattr(s, "sequences") else s.strips)

    for scene in bpy.data.scenes:
        se = scene.sequence_editor
        if not se:
            continue
        top = getattr(se, "strips_all", None) or getattr(se, "sequences_all", None) or []
        for s in top:
            try:
                if s.type == "MOVIE" and s.filepath:
                    dst = copy_file(absp(s.filepath), "vse")
                    if dst:
                        s.filepath = dst
                elif s.type == "IMAGE" and s.directory:
                    d = absp(s.directory)
                    dest_dir = os.path.join(EXT_DIR, f"vseimg_{_hash(d)}")
                    os.makedirs(dest_dir, exist_ok=True)
                    n = 0
                    for e in s.elements:
                        src = os.path.join(d, e.filename)
                        if os.path.isfile(src):
                            shutil.copy2(src, os.path.join(dest_dir, e.filename))
                            n += 1
                        else:
                            REPORT["missing"].append(src)
                    if n:
                        s.directory = dest_dir + os.sep
                        REPORT["copied"].append(f"{d} ({n} strip images)")
            except Exception as exc:
                warn(f"sequencer strip '{s.name}': {exc}")


def pass_blendcache():
    """Disk point caches live in blendcache_<blendname>/ next to the file."""
    if not ORIG_DIR:
        return
    src = os.path.join(ORIG_DIR, f"blendcache_{NAME}")
    if os.path.isdir(src):
        dst = os.path.join(OUT_DIR, f"blendcache_{NAME}")
        if not os.path.exists(dst):
            shutil.copytree(src, dst)
        REPORT["copied"].append(src + os.sep)


def remaining_external():
    left = []
    for p in bpy.utils.blend_paths(absolute=True, packed=False, local=False):
        ap = os.path.normpath(p)
        if not ap or ap.startswith(OUT_DIR):
            continue
        if not os.path.exists(ap):
            REPORT["missing"].append(ap)
        else:
            left.append(ap)
    for p in sorted(set(left)):
        warn(f"external file not bundled (still referenced from outside the bundle): {p}")


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #

def build_manifest(blend_name: str) -> dict:
    scene = bpy.context.scene
    rd = scene.render
    cyc = getattr(scene, "cycles", None)
    cam = scene.camera
    out_path = rd.filepath or ""
    # bpy.path.basename understands the '//' prefix (os.path treats it as UNC on Windows)
    basename = bpy.path.basename(out_path.replace("\\", "/"))
    frames = list(range(scene.frame_start, scene.frame_end + 1, max(1, scene.frame_step)))
    frame_names = {}
    for f in frames:
        try:
            frame_names[str(f)] = bpy.path.basename(rd.frame_path(frame=f).replace("\\", "/"))
        except Exception:
            pass
    manifest = {
        "blend_file": blend_name,
        "scene": scene.name,
        "blender_version": list(bpy.app.version),
        "blender_version_string": bpy.app.version_string,
        "engine": rd.engine,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "frame_step": scene.frame_step,
        "fps": rd.fps / rd.fps_base,
        "resolution": [rd.resolution_x, rd.resolution_y, rd.resolution_percentage],
        "output_path": out_path,
        "output_basename": basename,
        "file_format": rd.image_settings.file_format,
        "color_mode": rd.image_settings.color_mode,
        "color_depth": rd.image_settings.color_depth,
        "use_file_extension": rd.use_file_extension,
        "use_overwrite": rd.use_overwrite,
        "use_compositing": rd.use_compositing,
        "use_sequencer": rd.use_sequencer,
        "use_motion_blur": rd.use_motion_blur,
        "use_multiview": rd.use_multiview,
        "camera": cam.name if cam else None,
        "dof": bool(cam and cam.type == "CAMERA" and cam.data.dof.use_dof),
        "frame_filenames": frame_names,
    }
    if cyc is not None:
        manifest.update({
            "cycles_device_saved": cyc.device,
            "samples": cyc.samples,
            "use_adaptive_sampling": cyc.use_adaptive_sampling,
            "use_denoising": cyc.use_denoising,
            "denoiser": getattr(cyc, "denoiser", None),
            "time_limit": getattr(cyc, "time_limit", 0),
            "use_persistent_data": rd.use_persistent_data,
        })
    return manifest


# --------------------------------------------------------------------------- #

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(EXT_DIR, exist_ok=True)
    bundle_blend = os.path.join(OUT_DIR, NAME + ".blend")

    # 1. move the file into the bundle first so '//' now means the bundle dir
    bpy.ops.wm.save_as_mainfile(filepath=bundle_blend, compress=True, copy=False, relative_remap=True)
    log(f"saved working copy to {bundle_blend}")

    # 2/3. pack + copy
    for fn in (pass_libraries, pass_pack_all, pass_images, pass_movieclips, pass_sounds_fonts_volumes,
               pass_cache_files, pass_modifiers, pass_sequencer, pass_blendcache):
        try:
            fn()
        except Exception as exc:
            REPORT["errors"].append(f"{fn.__name__}: {exc}")
            log(f"ERROR in {fn.__name__}: {exc}\n{traceback.format_exc()}")

    # 4. relative + final save
    try:
        bpy.ops.file.make_paths_relative()
    except Exception as exc:
        warn(f"make_paths_relative: {exc}")
    remaining_external()
    bpy.ops.wm.save_as_mainfile(filepath=bundle_blend, compress=True, copy=False, relative_remap=False)
    for stale in glob.glob(os.path.join(OUT_DIR, "*.blend1")):
        os.remove(stale)

    # 5. manifest + report
    manifest = build_manifest(os.path.basename(bundle_blend))
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    REPORT["missing"] = sorted(set(REPORT["missing"]))
    with open(os.path.join(OUT_DIR, "pack_report.json"), "w", encoding="utf-8") as fh:
        json.dump(REPORT, fh, indent=1)
    log(f"packed={len(REPORT['packed'])} copied={len(REPORT['copied'])} "
        f"missing={len(REPORT['missing'])} warnings={len(REPORT['warnings'])} errors={len(REPORT['errors'])}")
    if REPORT["errors"]:
        sys.exit(2)


main()
