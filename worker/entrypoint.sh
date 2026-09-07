#!/bin/bash
# Container entrypoint for a Cloud Render worker.
#
# 1. Make sure the Blender version the artist is running locally is installed
#    (the image bakes one version; a different one is downloaded on demand so
#    the .blend always opens in the same release it was saved from).
# 2. Hand over to render_worker.py which does the actual work.
#
# Vast.ai starts the container with the env vars passed at instance creation
# (all CR_* variables) plus CONTAINER_ID (the Vast instance id).
set -uo pipefail

echo "[entrypoint] $(date -u +%FT%TZ) starting cloud render worker"
echo "[entrypoint] job=${CR_JOB_ID:-?} worker=${CR_WORKER_INDEX:-?} frames=${CR_FRAME_START:-?}..${CR_FRAME_END:-?} step=${CR_FRAME_STEP:-1}"

if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader || true
else
    echo "[entrypoint] WARNING: nvidia-smi not found - GPU may not be available"
fi

if [ -n "${CR_BLENDER_VERSION:-}" ]; then
    if ! /opt/worker/install_blender.sh "${CR_BLENDER_VERSION}"; then
        echo "[entrypoint] WARNING: could not install Blender ${CR_BLENDER_VERSION}; using baked version"
        /opt/blender/current/blender --version | head -1 || true
    fi
fi

mkdir -p /work
cd /work
exec python3 -u /opt/worker/render_worker.py
