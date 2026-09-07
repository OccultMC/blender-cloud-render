"""Background-Blender test: install the extension, register, pack a scene, ping R2/Vast.

    blender -b --python tests/test_addon.py -- --zip dist/cloud_render-1.0.0.zip [--env path/to/.env] [--no-net]
"""
import os
import sys
import json
import traceback

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ZIP = ""
ENV = ""
NET = True
i = 0
while i < len(argv):
    if argv[i] == "--zip":
        ZIP = argv[i + 1]; i += 2
    elif argv[i] == "--env":
        ENV = argv[i + 1]; i += 2
    elif argv[i] == "--no-net":
        NET = False; i += 1
    else:
        i += 1

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "_out")
os.makedirs(OUT, exist_ok=True)
FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# --------------------------------------------------------------------------- install
if ZIP:
    bpy.ops.extensions.package_install_files(filepath=os.path.abspath(ZIP), repo="user_default", enable_on_install=True)
mod_name = None
for name in list(sys.modules):
    if name.endswith(".cloud_render") and name.startswith("bl_ext."):
        mod_name = name
        break
check(mod_name is not None, f"extension module imported as {mod_name}")
if mod_name is None:
    sys.exit(1)
if not bpy.context.preferences.addons.get(mod_name):
    bpy.ops.preferences.addon_enable(module=mod_name)
check(bpy.context.preferences.addons.get(mod_name) is not None, "extension enabled in preferences")
check(hasattr(bpy.types, "CLOUDRENDER_PT_main"), "panel class registered")
check(hasattr(bpy.types.Scene, "cloud_render"), "Scene.cloud_render property registered")
check(hasattr(bpy.ops.cloudrender, "render_animation"), "cloudrender.render_animation operator registered")
check(bpy.types.TOPBAR_MT_render.draw.__name__ == "_draw_render_menu", "Render menu draw overridden")
kc = bpy.context.window_manager.keyconfigs.addon
km = kc.keymaps.get("Screen") if kc else None
check(km is not None and any(k.idname == "cloudrender.render_animation" and k.ctrl for k in km.keymap_items),
      "Ctrl+F12 addon keymap installed")

ext = sys.modules[mod_name]
planner = ext.planner
vast = ext.vast
r2 = ext.r2
packer = ext.packer

# --------------------------------------------------------------------------- planner
plan = planner.split_frames(1, 100, 1, 8)
check(len(plan) == 8 and sum(len(p["frames"]) for p in plan) == 100, "100 frames / 8 workers covers all frames")
check(max(len(p["frames"]) for p in plan) == 13 and plan[-1]["frames"][-1] == 100, "ceil split: 13 per worker, last ends at 100")
check(len(planner.split_frames(1, 3, 1, 20)) == 3, "20 workers for 3 frames collapses to 3 workers")
plan = planner.split_frames(1, 10, 3, 2)
check([p["frames"] for p in plan] == [[1, 4], [7, 10]], f"frame_step honoured: {[p['frames'] for p in plan]}")
check(planner.split_frames(5, 4, 1, 3) == [], "empty range -> empty plan")

# --------------------------------------------------------------------------- gpu regex
ok_names = ["RTX 4090", "RTX 3080 Ti", "RTX 2080 Ti", "RTX 5090", "RTX 4070 Ti Super", "RTX_3060", "GeForce RTX 5080"]
bad_names = ["RTX A6000", "RTX 6000Ada", "RTX PRO 6000", "A100 PCIE", "GTX 1080 Ti", "RTX 4000 Ada", "Tesla V100"]
check(all(vast.GEOFORCE_RTX_RE.match(n.replace("_", " ")) if hasattr(vast, "GEOFORCE_RTX_RE") else vast.GEFORCE_RTX_RE.match(n.replace("_", " ")) for n in ok_names), "GeForce RTX names accepted")
check(not any(vast.GEFORCE_RTX_RE.match(n) for n in bad_names), "non-GeForce cards rejected")
check(vast.min_driver_for((5, 2, 1)) == 575 and vast.min_driver_for((4, 5, 0)) == 470, "OptiX driver table")

# --------------------------------------------------------------------------- pack
try:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 8
    scene.cycles.use_denoising = True
    scene.render.use_motion_blur = True
    scene.frame_start, scene.frame_end = 1, 12
    scene.render.resolution_x, scene.render.resolution_y = 64, 64
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = "//frames/shot_"
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    bpy.ops.object.camera_add(location=(0, -6, 0), rotation=(1.5708, 0, 0))
    cam = bpy.context.active_object
    cam.data.dof.use_dof = True
    scene.camera = cam
    bpy.ops.object.light_add(type="SUN")
    # external texture on disk
    tex_path = os.path.join(OUT, "textures", "checker.png")
    os.makedirs(os.path.dirname(tex_path), exist_ok=True)
    img = bpy.data.images.new("checker", 32, 32)
    img.generated_type = "COLOR_GRID"
    img.filepath_raw = tex_path
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)
    img = bpy.data.images.load(tex_path)
    img.name = "checker"
    mat = bpy.data.materials.new("m")
    mat.use_nodes = True
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = img
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    cube.data.materials.append(mat)
    cube.keyframe_insert("location", frame=1)
    cube.location.x = 2
    cube.keyframe_insert("location", frame=12)
    blend = os.path.join(OUT, "scene_test.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    check(bpy.data.images["checker"].packed_file is None, "texture is external before packing")

    tmp = packer.snapshot_session(blend)
    check(os.path.exists(tmp), "session snapshot saved next to the blend")
    bundle_dir = os.path.join(OUT, "bundle")
    logs = []
    manifest, report = packer.run_pack(bpy.app.binary_path, tmp, bundle_dir, "scene_test", os.path.dirname(blend), logs.append)
    print("\n".join(logs))
    check(os.path.exists(os.path.join(bundle_dir, "scene_test.blend")), "bundle .blend written")
    check(manifest["frame_start"] == 1 and manifest["frame_end"] == 12, "manifest frame range")
    check(manifest["use_denoising"] is True and manifest["use_motion_blur"] is True and manifest["dof"] is True,
          f"manifest carries denoise/motion blur/DOF: {manifest['use_denoising']} {manifest['use_motion_blur']} {manifest['dof']}")
    check(manifest["output_basename"] == "shot_" and manifest["frame_filenames"]["7"] == "shot_0007.png",
          f"output naming: {manifest['output_basename']} {manifest['frame_filenames'].get('7')}")
    check(not report.get("missing") and not report.get("errors"), f"no missing files / errors: {report.get('missing')} {report.get('errors')}")
    zip_path = os.path.join(OUT, "bundle.zip")
    size = packer.zip_bundle(bundle_dir, zip_path, logs.append)
    check(size > 0 and os.path.exists(zip_path), "bundle zipped")
    # verify the packed copy really has the texture embedded
    bpy.ops.wm.open_mainfile(filepath=os.path.join(bundle_dir, "scene_test.blend"))
    check(bpy.data.images["checker"].packed_file is not None, "texture packed inside bundle .blend")
    check(bpy.context.scene.cycles.use_denoising and bpy.context.scene.render.use_motion_blur, "render settings preserved in bundle")
    os.remove(tmp)
except Exception:
    traceback.print_exc()
    FAILS.append("pack test crashed")

# --------------------------------------------------------------------------- pack: linked library + image sequence
try:
    lib_dir = os.path.join(OUT, "libs")
    os.makedirs(lib_dir, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_ico_sphere_add()
    bpy.context.active_object.name = "LibSphere"
    lib_path = os.path.join(lib_dir, "lib.blend")
    bpy.ops.wm.save_as_mainfile(filepath=lib_path)

    seq_dir = os.path.join(OUT, "seq")
    os.makedirs(seq_dir, exist_ok=True)
    for n in range(1, 4):
        im = bpy.data.images.new("f", 8, 8)
        im.filepath_raw = os.path.join(seq_dir, f"plate_{n:03d}.png")
        im.file_format = "PNG"
        im.save()
        bpy.data.images.remove(im)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    with bpy.data.libraries.load(lib_path, link=True) as (src, dst):
        dst.objects = ["LibSphere"]
    for ob in dst.objects:
        scene.collection.objects.link(ob)
    seq = bpy.data.images.load(os.path.join(seq_dir, "plate_001.png"))
    seq.source = "SEQUENCE"
    mat = bpy.data.materials.new("seqmat")
    mat.use_nodes = True
    node = mat.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = seq
    node.image_user.frame_duration = 3
    bpy.ops.mesh.primitive_plane_add()
    bpy.context.active_object.data.materials.append(mat)
    blend2 = os.path.join(OUT, "scene_linked.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend2)

    tmp2 = packer.snapshot_session(blend2)
    bundle2 = os.path.join(OUT, "bundle2")
    logs = []
    manifest2, report2 = packer.run_pack(bpy.app.binary_path, tmp2, bundle2, "scene_linked", os.path.dirname(blend2), logs.append)
    print("\n".join(logs))
    ext_dir = os.path.join(bundle2, "_ext")
    seq_dirs = [d for d in os.listdir(ext_dir) if d.startswith("seq_")] if os.path.isdir(ext_dir) else []
    check(len(seq_dirs) == 1 and len(os.listdir(os.path.join(ext_dir, seq_dirs[0]))) == 3,
          f"image sequence copied into _ext ({seq_dirs})")
    check(not report2.get("missing") and not report2.get("errors"), f"linked scene: no missing/errors {report2.get('missing')} {report2.get('errors')}")
    bpy.ops.wm.open_mainfile(filepath=os.path.join(bundle2, "scene_linked.blend"))
    libs = list(bpy.data.libraries)
    check(libs and all(l.packed_file is not None for l in libs), f"linked library packed into bundle ({[l.filepath for l in libs]})")
    simg = bpy.data.images.get("plate_001.png")
    # Blender keeps native separators on Windows; BLI_path_abs normalises '\' -> '/' on Linux workers.
    check(simg is not None and simg.filepath.replace("\\", "/").startswith("//_ext/") and os.path.exists(bpy.path.abspath(simg.filepath)),
          f"sequence re-pointed to bundle-relative path: {simg.filepath if simg else None}")
    check(bpy.data.objects.get("LibSphere") is not None, "linked object present in bundle")
    os.remove(tmp2)
except Exception:
    traceback.print_exc()
    FAILS.append("linked/sequence pack test crashed")

# --------------------------------------------------------------------------- network (read-only)
if NET:
    creds = {}
    if ENV and os.path.exists(ENV):
        creds = ext.prefs.load_env_file(ENV)
    vast_key = creds.get("VAST_API_KEY") or ext.prefs._read_vast_key_file()
    try:
        client = vast.VastClient(vast_key)
        me = client.whoami()
        check("email" in me or "id" in me, f"Vast whoami ok (credit ${float(me.get('credit') or 0):.2f})")
        offers = client.search_offers(min_vram_mb=8 * 1024, disk_gb=40, max_dph=0.6, min_reliability=0.95,
                                      min_driver=575, series=["20", "30", "40", "50"], min_inet_down=200)
        picked = vast.VastClient.pick_offers(offers, 4)
        print(f"  {len(offers)} matching offers; cheapest 4:")
        for o in picked:
            print(f"   offer {o['id']} {o['gpu_name']} {o.get('gpu_ram')}MB ${o['dph_total']:.3f}/h {o.get('geolocation')} drv {o.get('driver_version')} rel {o.get('reliability'):.2f} machine {o.get('machine_id')}")
        check(len(offers) > 0, "Vast search returns RTX offers with driver >= 575")
        check(all(vast.GEFORCE_RTX_RE.match((o['gpu_name'] or '').replace('_', ' ')) for o in offers), "all offers are GeForce RTX")
        check(all(vast._parse_driver(str(o.get('driver_version'))) >= 575 for o in offers), "all offers meet the driver floor")
    except Exception as exc:
        traceback.print_exc()
        FAILS.append(f"vast test: {exc}")
    try:
        c = r2.R2Client(creds.get("R2_ACCOUNT_ID", ""), creds.get("R2_ACCESS_KEY_ID", ""),
                        creds.get("R2_SECRET_ACCESS_KEY", ""), creds.get("R2_BUCKET_NAME", ""))
        print("  " + c.test())
        key = "blender-cloud-render/_selftest/hello.json"
        c.put_json(key, {"hello": "world", "n": 1})
        got = c.get_json(key)
        check(got == {"hello": "world", "n": 1}, "R2 put/get JSON round-trip (SigV4 OK)")
        listed = [o["key"] for o in c.list("blender-cloud-render/_selftest/")]
        check(key in listed, "R2 list finds the object")
        # small file upload through the streaming PUT path
        p = os.path.join(OUT, "bundle.zip")
        if os.path.exists(p):
            c.upload_file(p, "blender-cloud-render/_selftest/bundle.zip")
            check(c.exists("blender-cloud-render/_selftest/bundle.zip"), "R2 streaming file upload")
        n = c.delete_prefix("blender-cloud-render/_selftest/")
        check(n >= 1 and not c.list("blender-cloud-render/_selftest/"), f"R2 cleanup deleted {n} objects")
    except Exception as exc:
        traceback.print_exc()
        FAILS.append(f"r2 test: {exc}")

print("\n==== %d failure(s) ====" % len(FAILS))
for f in FAILS:
    print(" - " + f)
sys.exit(1 if FAILS else 0)
