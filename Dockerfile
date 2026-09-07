# Cloud Render worker image: headless Blender + Cycles (OptiX/CUDA) on Vast.ai.
#
# The Blender release baked here is only a default; entrypoint.sh downloads the
# exact version the artist runs locally (CR_BLENDER_VERSION) at start-up so the
# .blend always opens in the release it was saved from.
FROM ubuntu:24.04

ARG BLENDER_VERSION=5.2.1
ARG DEBIAN_FRONTEND=noninteractive

# Libraries a headless Blender build links against, plus python for the worker.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils \
        python3 python3-pip \
        libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libxext6 \
        libgl1 libegl1 libxkbcommon0 libsm6 libice6 libgomp1 \
        libdbus-1-3 libwayland-client0 libdecor-0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages "boto3>=1.35" requests

COPY worker/ /opt/worker/
RUN chmod +x /opt/worker/*.sh && /opt/worker/install_blender.sh "${BLENDER_VERSION}"

# The NVIDIA container runtime injects libnvoptix.so.1 only when the
# 'graphics' capability is requested; without it Cycles cannot use OptiX.
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=all \
    PYTHONUNBUFFERED=1 \
    CR_DEVICE=OPTIX

WORKDIR /work
LABEL org.opencontainers.image.source="https://github.com/OccultMC/blender-cloud-render"
LABEL org.opencontainers.image.description="Headless Blender Cycles worker for the Cloud Render add-on"

# Vast.ai's ssh runtype runs 'onstart' instead of ENTRYPOINT; the add-on passes
# 'bash /opt/worker/entrypoint.sh' as onstart.  ENTRYPOINT covers plain docker.
ENTRYPOINT ["/opt/worker/entrypoint.sh"]
