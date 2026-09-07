# Runs inside Blender (``blender -b file.blend --python gpu_setup.py ...``)
# before the frames are rendered.  Forces Cycles onto the GPU using OptiX,
# falling back to CUDA and finally CPU if the driver cannot provide OptiX.
#
# Everything else (samples, denoising, motion blur, DOF, output format,
# colour management, compositing, ...) is left exactly as saved in the .blend
# so the cloud frames match what the artist would get locally.
import os
import sys

import bpy

PREFERRED = os.environ.get("CR_DEVICE", "OPTIX").upper()
ORDER = [PREFERRED] + [d for d in ("OPTIX", "CUDA") if d != PREFERRED]


def _refresh(prefs):
    # API name changed across versions; try the modern one first.
    for name in ("refresh_devices", "get_devices"):
        fn = getattr(prefs, name, None)
        if fn is not None:
            try:
                fn()
                return
            except Exception:
                pass


def pick_device():
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for dev_type in ORDER:
        try:
            prefs.compute_device_type = dev_type
        except TypeError:
            # Enum item not available in this build.
            continue
        _refresh(prefs)
        gpus = [d for d in prefs.devices if d.type == dev_type]
        if gpus:
            for d in prefs.devices:
                d.use = d.type == dev_type
            names = ", ".join(d.name for d in gpus)
            print(f"[gpu_setup] using {dev_type}: {names}", flush=True)
            return dev_type
        print(f"[gpu_setup] no {dev_type} devices found", flush=True)
    prefs.compute_device_type = "NONE"
    for d in prefs.devices:
        d.use = d.type == "CPU"
    print("[gpu_setup] WARNING: no GPU device available, rendering on CPU", flush=True)
    return "CPU"


def main():
    dev_type = pick_device()
    for scene in bpy.data.scenes:
        if scene.render.engine != "CYCLES":
            continue
        cyc = scene.cycles
        cyc.device = "GPU" if dev_type != "CPU" else "CPU"
        # The OptiX denoiser needs an OptiX device; keep denoising on but swap
        # the backend so the render does not fail on CUDA/CPU workers.
        if dev_type != "OPTIX" and getattr(cyc, "denoiser", "") == "OPTIX":
            cyc.denoiser = "OPENIMAGEDENOISE"
            print(f"[gpu_setup] scene '{scene.name}': OptiX denoiser -> OpenImageDenoise", flush=True)
        print(
            f"[gpu_setup] scene '{scene.name}': device={cyc.device} samples={cyc.samples} "
            f"denoise={cyc.use_denoising} denoiser={getattr(cyc, 'denoiser', '?')} "
            f"motion_blur={scene.render.use_motion_blur} "
            f"format={scene.render.image_settings.file_format} "
            f"res={scene.render.resolution_x}x{scene.render.resolution_y}@{scene.render.resolution_percentage}%",
            flush=True,
        )
    # Marker the worker parses to report which device actually rendered.
    print(f"[gpu_setup] DEVICE_USED={dev_type}", flush=True)
    try:
        with open(os.path.join(os.environ.get("CR_WORK", "/work"), "device_used.txt"), "w") as fh:
            fh.write(dev_type)
    except OSError:
        pass


main()
