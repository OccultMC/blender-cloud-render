# Blender Cloud Render

Render Cycles animations on the cheapest RTX 20/30/40/50 machines on Vast.ai,
straight from Blender's Render tab.

* **Render Image** always renders on your own machine.
* **Render Animation** (menu or `Ctrl+F12`) with *Render on Cloud* ticked packs
  the whole scene, rents *N* GPU workers, renders a contiguous whole-frame slice
  per worker with **Cycles + OptiX**, and streams the image sequence back through
  Cloudflare R2 into the scene's output folder as frames finish.

Everything in the .blend is used as-is: samples, denoising, motion blur, depth
of field, colour management, compositing, output format, file naming. The
worker only forces the Cycles device to GPU/OptiX (falling back to CUDA if the
host driver has no OptiX, and to CPU with a loud warning if there is no GPU).

```
Blender add-on                         Cloudflare R2                    Vast.ai worker (Docker)
--------------                         -------------                    -----------------------
pack scene  ─► bundle.zip ───────────► jobs/<id>/bundle.zip ──────────► download + unzip
split frames ─► rent N cheapest RTX ─► jobs/<id>/workers.json          blender -b scene.blend -s A -e B -a
poll status ◄──────────────────────── jobs/<id>/status/worker_n.json ◄─ heartbeat every 10 s
download ◄─────────────────────────── jobs/<id>/frames/shot_0001.png ◄─ upload each frame as saved
destroy leftovers                                                       self-destroy via Vast API
```

## Layout

| Path | What |
|---|---|
| `addon/cloud_render/` | Blender 4.2+ extension (`blender_manifest.toml`) |
| `worker/` | Container entrypoint, `render_worker.py`, `gpu_setup.py` |
| `Dockerfile` | Headless Blender + boto3 worker image |
| `.github/workflows/build-image.yml` | Builds and pushes `ghcr.io/occultmc/blender-cloud-render:latest` |
| `tests/test_addon.py` | Background-Blender test suite (register, plan, pack, R2, Vast) |

## Install the add-on

Build the extension zip with Blender itself (any 4.2+ build):

```powershell
& "D:\Program Files\Blender Foundation\Blender 5.2\blender.exe" --command extension build --source-dir addon\cloud_render --output-dir dist
```

Then in Blender: *Edit > Preferences > Get Extensions > (dropdown) Install from Disk...* and pick
`dist/cloud_render-1.0.0.zip`. The test suite installs it into the `user_default` repo automatically.

### Preferences (Edit > Preferences > Add-ons > Cloud Render)

| Field | Notes |
|---|---|
| Vast.ai API Key | falls back to `VAST_API_KEY` or `~/.config/vastai/vast_api_key` |
| R2 Account ID / Access Key / Secret / Bucket | fall back to `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` |
| R2 Prefix | folder inside the bucket, default `blender-cloud-render` |
| Worker Image | `ghcr.io/occultmc/blender-cloud-render:latest` |
| GHCR Token | only if the package is private (Vast pulls with `-u user -p token ghcr.io`) |
| Pin image digest | resolves `:latest` to `@sha256:` so Vast hosts never run a stale cached image |

*Import .env* fills everything from a `.env` file (defaults to the Hypervision scraper one).
*Test Vast.ai* / *Test R2* verify the credentials. Blender's **Allow Online Access**
(Preferences > System) must be enabled.

## Use

1. Save the .blend. Set the output path/format as you would locally (any image
   format; video formats are refused because each worker renders a slice).
2. Render properties (Cycles) > **Render on Cloud** > tick it.
3. Set **Number of Workers** (0-20; 0 = render locally). The panel shows how the
   frames split, e.g. `100 frames over 8 workers (12-13 frames each)`.
4. Optional filters: RTX series toggles, min VRAM, max $/hour; *Worker Options*
   has reliability, download speed, disk, retries, auto-download, self-destroy.
5. **Find Workers** previews the cheapest N offers and the total $/hour.
6. **Render > Render Animation on Cloud** (or `Ctrl+F12`, or the panel button).

The *Cloud Job* sub-panel shows the stage, a progress bar, per-worker state
(GPU, price, current frame, device actually used), warnings from packing, and
the log. Frames land in the same folder and with the same names a local render
would produce. **Cancel** destroys every instance. The job is recorded in the
.blend (`cloud_render_jobs/<job-id>/job.json` next to the file) so re-opening
the file resumes monitoring.

### How frames are split

`ceil(frames / workers)` frames per worker, contiguous, honouring the frame
step. 100 frames on 8 workers = 7 x 13 + 1 x 9. If there are more workers than
frames, the worker count is reduced. If fewer offers than workers match the
filters, the plan is re-cut onto the available machines.

### What gets packed

`pack_libraries` + `pack_all` embed linked .blend libraries, images (incl. UDIM
tiles), sounds, fonts and OpenVDB volumes into the bundle .blend. Things Blender
cannot pack are copied into `_ext/` and re-pointed: image sequences, movies,
movie clips, Alembic/USD cache files, fluid/ocean/mesh caches, sequencer strips,
external point caches, and the `blendcache_<name>/` disk cache folder. Anything
still referenced from outside the bundle is listed as a warning in the panel.

### Worker selection

Vast.ai offers are queried for single-GPU, verified, rentable NVIDIA hosts with
the requested VRAM/disk/reliability/bandwidth, sorted by `$/hour`, then filtered
client-side to GeForce RTX 20/30/40/50 cards (workstation RTX A/Ada/PRO cards
are excluded) whose driver meets the Cycles OptiX floor for your Blender version
(470 for 4.x, 570 for 5.0/5.1, 575 for 5.2). The cheapest N on distinct hosts
win. Dead or stalled workers are destroyed and their remaining frames
re-dispatched to the next cheapest host (up to *Retries per worker*).

## Worker image

`Dockerfile` bakes a default Blender release; `entrypoint.sh` downloads the exact
version the add-on reports (`CR_BLENDER_VERSION`) from download.blender.org if it
differs, so files always open in the release they were saved from.
`NVIDIA_DRIVER_CAPABILITIES=all` is required: the NVIDIA runtime only injects
`libnvoptix.so.1` with the `graphics` capability.

The GitHub Actions workflow builds and pushes the image on every push to `main`
that touches `Dockerfile` or `worker/`. A package published by Actions starts
**private**; make it public once at
<https://github.com/users/OccultMC/packages/container/blender-cloud-render/settings>
or set a GHCR token in the add-on preferences.

## Tests

```powershell
& "D:\Program Files\Blender Foundation\Blender 5.2\blender.exe" -b --python tests\test_addon.py -- --zip dist\cloud_render-1.0.0.zip --env D:\GeoAxis\Hypervision\VPS_Scraper\.env
```

Add `--no-net` to skip the Vast/R2 round-trips.
