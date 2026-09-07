"""Vast.ai REST client (offers, create, list, destroy-with-verify) + GHCR helpers.

Only the standard library is used so it runs inside Blender's Python.
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Optional

VAST_BASE = "https://console.vast.ai/api/v0"

# GeForce RTX 20/30/40/50 series, e.g. "RTX 4090", "RTX 3080 Ti", "RTX 4070 Ti Super".
GEFORCE_RTX_RE = re.compile(r"^(?:GeForce\s+)?RTX\s*([2-5])0[5-9]0(?:\s*(?:Ti|Super|SUPER|D))*$", re.I)

# Minimum NVIDIA driver for Cycles OptiX per Blender major.minor (from the manual).
OPTIX_MIN_DRIVER = {
    (4, 2): 470, (4, 3): 470, (4, 4): 470, (4, 5): 470,
    (5, 0): 570, (5, 1): 570, (5, 2): 575,
}


class VastError(RuntimeError):
    pass


def _parse_driver(v: str) -> float:
    m = re.match(r"(\d+)(?:\.(\d+))?", v or "")
    if not m:
        return 0.0
    return float(m.group(1)) + float(m.group(2) or 0) / 1000.0


def min_driver_for(blender_version: tuple) -> int:
    key = (blender_version[0], blender_version[1])
    if key in OPTIX_MIN_DRIVER:
        return OPTIX_MIN_DRIVER[key]
    return 575 if blender_version >= (5, 2) else (570 if blender_version[0] >= 5 else 470)


class VastClient:
    def __init__(self, api_key: str, timeout: float = 60.0):
        self.api_key = api_key.strip()
        self.timeout = timeout
        if not self.api_key:
            raise VastError("Vast.ai API key is empty")

    def _req(self, method: str, path: str, body: Optional[dict] = None, params: Optional[dict] = None, retries: int = 3):
        url = f"{VAST_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "blender-cloud-render/1.0",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        last = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                    return resp.status, (json.loads(raw) if raw.strip() else {})
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                if exc.code == 429 or exc.code >= 500:
                    last = VastError(f"{method} {path} -> {exc.code}: {raw[:300]}")
                    time.sleep(2.0 * (attempt + 1))
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {"msg": raw[:300]}
                return exc.code, payload
            except (urllib.error.URLError, OSError) as exc:
                last = VastError(f"{method} {path}: {exc}")
                time.sleep(2.0 * (attempt + 1))
        raise last or VastError(f"{method} {path} failed")

    # ---------------------------------------------------------------- account
    def whoami(self) -> dict:
        code, data = self._req("GET", "/users/current/")
        if code != 200:
            raise VastError(f"auth failed ({code}): {data.get('msg') or data.get('error') or data}")
        return data

    # ----------------------------------------------------------------- offers
    def search_offers(self, *, min_vram_mb: int, disk_gb: int, max_dph: float, min_reliability: float,
                      min_driver: int, series: Iterable[str], min_inet_down: int = 100,
                      geolocations: Optional[list] = None, limit: int = 400) -> list[dict]:
        """Cheapest single-GPU GeForce RTX offers first, filtered client-side by series/driver."""
        query = {
            "limit": limit,
            "type": "ondemand",
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "num_gpus": {"eq": 1},
            "gpu_arch": {"eq": "nvidia"},
            "gpu_ram": {"gte": int(min_vram_mb)},
            "disk_space": {"gte": float(disk_gb)},
            "reliability": {"gte": float(min_reliability)},
            "inet_down": {"gte": float(min_inet_down)},
            "cuda_max_good": {"gte": 12.0},
            "order": [["dph_total", "asc"]],
        }
        if max_dph > 0:
            query["dph_total"] = {"lte": float(max_dph)}
        if geolocations:
            query["geolocation"] = {"in": list(geolocations)}
        code, data = self._req("POST", "/bundles/", body=query)
        if code != 200:
            raise VastError(f"search offers failed ({code}): {data.get('msg') or data.get('error') or data}")
        offers = data.get("offers") or []
        wanted = {str(s) for s in series}
        out = []
        for o in offers:
            name = (o.get("gpu_name") or "").replace("_", " ").strip()
            m = GEFORCE_RTX_RE.match(name)
            if not m or m.group(1) + "0" not in wanted:
                continue
            if _parse_driver(str(o.get("driver_version", ""))) < min_driver:
                continue
            out.append(o)
        out.sort(key=lambda o: (float(o.get("dph_total") or 9e9), -float(o.get("reliability") or 0)))
        return out

    @staticmethod
    def pick_offers(offers: list[dict], n: int) -> list[dict]:
        """Take the n cheapest, preferring distinct host machines."""
        chosen, seen = [], set()
        for o in offers:
            if len(chosen) >= n:
                break
            if o.get("machine_id") in seen:
                continue
            seen.add(o.get("machine_id"))
            chosen.append(o)
        if len(chosen) < n:
            for o in offers:
                if len(chosen) >= n:
                    break
                if o not in chosen:
                    chosen.append(o)
        return chosen

    # -------------------------------------------------------------- instances
    def create_instance(self, offer_id: int, *, image: str, env: dict, onstart: str, disk_gb: float,
                        label: str, image_login: str = "") -> int:
        body = {
            "client_id": "me",
            "image": image,
            "env": {k: str(v) for k, v in env.items()},
            "onstart": onstart,
            "disk": float(disk_gb),
            "label": label,
            "runtype": "ssh",
            "python_utf8": True,
            "lang_utf8": True,
            "cancel_unavail": True,
        }
        if image_login:
            body["image_login"] = image_login
        code, data = self._req("PUT", f"/asks/{int(offer_id)}/", body=body, retries=2)
        if code != 200 or not data.get("success", True):
            raise VastError(f"create instance on offer {offer_id} failed ({code}): {data.get('msg') or data.get('error') or data}")
        new_id = data.get("new_contract") or data.get("id")
        if not new_id:
            # The create may have gone through even if the response is odd:
            # look the instance up by its unique label before giving up.
            time.sleep(5)
            for inst in self.list_instances():
                if inst.get("label") == label:
                    return int(inst["id"])
            raise VastError(f"create instance returned no id: {data}")
        return int(new_id)

    def list_instances(self) -> list[dict]:
        code, data = self._req("GET", "/instances/", params={"owner": "me"})
        if code != 200:
            raise VastError(f"list instances failed ({code}): {data.get('msg') or data}")
        inst = data.get("instances", [])
        return inst if isinstance(inst, list) else [inst]

    def destroy_instance(self, instance_id: int, verify_rounds: int = 3) -> bool:
        """DELETE then re-list until the id is gone (Vast sometimes 'succeeds' without destroying)."""
        for attempt in range(verify_rounds):
            code, data = self._req("DELETE", f"/instances/{int(instance_id)}/", retries=2)
            if code not in (200, 404):
                time.sleep(3)
            time.sleep(5)
            try:
                live = {int(i.get("id")) for i in self.list_instances() if i.get("id") is not None}
            except VastError:
                if code in (200, 404):
                    return True
                continue
            if int(instance_id) not in live:
                return True
        return False

    def destroy_many(self, ids: Iterable[int]) -> list[int]:
        """Destroy each id individually; return ids that are still alive afterwards."""
        remaining = []
        for iid in ids:
            try:
                if not self.destroy_instance(int(iid)):
                    remaining.append(int(iid))
            except VastError:
                remaining.append(int(iid))
        return remaining


# --------------------------------------------------------------------------- #
# GHCR: pin :tag to a digest so Vast hosts do not serve a stale cached image.
# --------------------------------------------------------------------------- #

def resolve_image_digest(image: str, ghcr_user: str = "", ghcr_token: str = "", timeout: float = 30.0) -> str:
    """ghcr.io/owner/repo:tag -> ghcr.io/owner/repo@sha256:...  (returns input on failure)."""
    if not image.startswith("ghcr.io/") or "@sha256:" in image:
        return image
    repo_tag = image[len("ghcr.io/"):]
    repo, _, tag = repo_tag.partition(":")
    tag = tag or "latest"
    try:
        token_url = f"https://ghcr.io/token?scope=repository:{repo}:pull"
        req = urllib.request.Request(token_url)
        if ghcr_token:
            cred = base64.b64encode(f"{ghcr_user or 'token'}:{ghcr_token}".encode()).decode()
            req.add_header("Authorization", f"Basic {cred}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            token = json.loads(resp.read().decode()).get("token", "")
        req = urllib.request.Request(f"https://ghcr.io/v2/{repo}/manifests/{tag}", method="HEAD")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", ", ".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ]))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            digest = resp.headers.get("Docker-Content-Digest", "")
        if digest.startswith("sha256:"):
            return f"ghcr.io/{repo}@{digest}"
    except Exception:
        pass
    return image


def ghcr_login_string(user: str, token: str) -> str:
    """Vast 'image_login' value for a private GHCR image."""
    if not token:
        return ""
    return f"-u {user or 'token'} -p {token} ghcr.io"
