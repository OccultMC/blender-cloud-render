#!/bin/bash
# Install a specific Blender release into /opt/blender/<version> and point
# /opt/blender/current at it.  Idempotent: skips the download when the version
# is already present.  Usage: install_blender.sh 5.2.1
set -euo pipefail

VERSION="${1:?usage: install_blender.sh <x.y.z>}"
ROOT="/opt/blender"
DEST="${ROOT}/${VERSION}"
MAJOR_MINOR="$(echo "${VERSION}" | cut -d. -f1,2)"
TARBALL="blender-${VERSION}-linux-x64.tar.xz"
URL="https://download.blender.org/release/Blender${MAJOR_MINOR}/${TARBALL}"

mkdir -p "${ROOT}"
if [ -x "${DEST}/blender" ]; then
    echo "[install_blender] ${VERSION} already installed"
else
    echo "[install_blender] downloading ${URL}"
    TMP="$(mktemp -d)"
    curl -fL --retry 5 --retry-delay 3 -o "${TMP}/${TARBALL}" "${URL}"
    mkdir -p "${DEST}"
    tar -xJf "${TMP}/${TARBALL}" -C "${DEST}" --strip-components=1
    rm -rf "${TMP}"
    echo "[install_blender] installed ${VERSION} -> ${DEST}"
fi

ln -sfn "${DEST}" "${ROOT}/current"
"${ROOT}/current/blender" --version | head -1
