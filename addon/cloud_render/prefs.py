"""Add-on preferences: Vast.ai key, R2 credentials, worker image."""
from __future__ import annotations

import os
from dataclasses import dataclass

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import AddonPreferences

DEFAULT_IMAGE = "ghcr.io/occultmc/blender-cloud-render:latest"
DEFAULT_PREFIX = "blender-cloud-render"


def _read_vast_key_file() -> str:
    for p in (os.path.expanduser("~/.config/vastai/vast_api_key"), os.path.expanduser("~/.vast_api_key")):
        try:
            with open(p, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            continue
    return ""


@dataclass
class Credentials:
    vast_key: str = ""
    r2_account: str = ""
    r2_access_key: str = ""
    r2_secret_key: str = ""
    r2_bucket: str = ""
    r2_endpoint: str = ""
    r2_prefix: str = DEFAULT_PREFIX
    image: str = DEFAULT_IMAGE
    ghcr_user: str = ""
    ghcr_token: str = ""
    pin_digest: bool = True

    def missing(self) -> list:
        out = []
        if not self.vast_key:
            out.append("Vast.ai API key")
        if not self.r2_account and not self.r2_endpoint:
            out.append("R2 account ID")
        if not self.r2_access_key:
            out.append("R2 access key")
        if not self.r2_secret_key:
            out.append("R2 secret key")
        if not self.r2_bucket:
            out.append("R2 bucket")
        if not self.image:
            out.append("worker image")
        return out


def get_prefs(context=None):
    context = context or bpy.context
    return context.preferences.addons[__package__].preferences


def resolve_credentials(prefs) -> Credentials:
    """Preferences first, then environment variables, then ~/.config/vastai."""
    env = os.environ.get
    return Credentials(
        vast_key=prefs.vast_api_key.strip() or env("VAST_API_KEY", "").strip() or _read_vast_key_file(),
        r2_account=prefs.r2_account_id.strip() or env("R2_ACCOUNT_ID", "").strip(),
        r2_access_key=prefs.r2_access_key.strip() or env("R2_ACCESS_KEY_ID", "").strip(),
        r2_secret_key=prefs.r2_secret_key.strip() or env("R2_SECRET_ACCESS_KEY", "").strip(),
        r2_bucket=prefs.r2_bucket.strip() or env("R2_BUCKET_NAME", "").strip(),
        r2_endpoint=prefs.r2_endpoint.strip(),
        r2_prefix=(prefs.r2_prefix.strip() or DEFAULT_PREFIX).strip("/"),
        image=prefs.docker_image.strip() or DEFAULT_IMAGE,
        ghcr_user=prefs.ghcr_user.strip() or env("GHCR_USER", "").strip(),
        ghcr_token=prefs.ghcr_token.strip() or env("GHCR_PAT", "").strip(),
        pin_digest=prefs.pin_image_digest,
    )


def load_env_file(path: str) -> dict:
    values = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


class CLOUDRENDER_preferences(AddonPreferences):
    bl_idname = __package__

    vast_api_key: StringProperty(name="Vast.ai API Key", subtype="PASSWORD",
                                 description="Leave empty to use VAST_API_KEY or ~/.config/vastai/vast_api_key")
    r2_account_id: StringProperty(name="R2 Account ID", description="Cloudflare account id (part of the R2 endpoint)")
    r2_access_key: StringProperty(name="R2 Access Key ID")
    r2_secret_key: StringProperty(name="R2 Secret Access Key", subtype="PASSWORD")
    r2_bucket: StringProperty(name="R2 Bucket")
    r2_prefix: StringProperty(name="R2 Prefix", default=DEFAULT_PREFIX,
                              description="Folder inside the bucket where jobs/<job-id>/frames live")
    r2_endpoint: StringProperty(name="R2 Endpoint (optional)",
                                description="Override the endpoint URL; default is https://<account>.r2.cloudflarestorage.com")
    docker_image: StringProperty(name="Worker Image", default=DEFAULT_IMAGE)
    ghcr_user: StringProperty(name="GHCR User", default="occultmc")
    ghcr_token: StringProperty(name="GHCR Token (private image only)", subtype="PASSWORD",
                               description="Personal access token with read:packages; leave empty for a public image")
    pin_image_digest: BoolProperty(name="Pin image digest", default=True,
                                   description="Resolve :latest to a sha256 digest so Vast hosts do not run a stale cached image")
    env_file: StringProperty(name=".env file", subtype="FILE_PATH",
                             default=r"D:\GeoAxis\Hypervision\VPS_Scraper\.env",
                             description="Import VAST_API_KEY / R2_* values from a .env file")

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.use_property_split = True
        box = col.box()
        box.label(text="Vast.ai", icon="WORLD")
        box.prop(self, "vast_api_key")
        box = col.box()
        box.label(text="Cloudflare R2", icon="FILE_FOLDER")
        box.prop(self, "r2_account_id")
        box.prop(self, "r2_access_key")
        box.prop(self, "r2_secret_key")
        box.prop(self, "r2_bucket")
        box.prop(self, "r2_prefix")
        box.prop(self, "r2_endpoint")
        box = col.box()
        box.label(text="Worker image", icon="PACKAGE")
        box.prop(self, "docker_image")
        box.prop(self, "ghcr_user")
        box.prop(self, "ghcr_token")
        box.prop(self, "pin_image_digest")
        box = col.box()
        box.label(text="Import", icon="IMPORT")
        box.prop(self, "env_file")
        row = layout.row(align=True)
        row.operator("cloudrender.load_env", icon="IMPORT")
        row.operator("cloudrender.test_vast", icon="CHECKMARK")
        row.operator("cloudrender.test_r2", icon="CHECKMARK")
        creds = resolve_credentials(self)
        missing = creds.missing()
        if missing:
            layout.label(text="Missing: " + ", ".join(missing), icon="ERROR")
        else:
            layout.label(text="All credentials resolved (preferences / environment / key file)", icon="CHECKMARK")
        if not getattr(bpy.app, "online_access", True):
            layout.label(text="Blender's 'Allow Online Access' is disabled (Preferences > System)", icon="ERROR")
