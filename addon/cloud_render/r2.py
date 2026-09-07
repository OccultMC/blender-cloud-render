"""Minimal S3 (SigV4) client for Cloudflare R2 using only the standard library.

Blender's bundled Python has no boto3, so the add-on signs requests itself.
Supports PUT (single and multipart), GET, LIST (v2) and DELETE - everything
the add-on needs to ship a bundle up and pull frames back down.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import http.client
import io
import os
import ssl
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Callable, Iterable, Optional

UNSIGNED = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MULTIPART_THRESHOLD = 256 * 1024 * 1024
PART_SIZE = 64 * 1024 * 1024


class R2Error(RuntimeError):
    pass


def _quote(s: str, safe: str = "~") -> str:
    return urllib.parse.quote(s, safe=safe)


class R2Client:
    def __init__(self, account_id: str, access_key: str, secret_key: str, bucket: str,
                 endpoint: str = "", region: str = "auto", timeout: float = 120.0):
        endpoint = (endpoint or "").strip() or f"https://{account_id.strip()}.r2.cloudflarestorage.com"
        self.endpoint = endpoint.rstrip("/")
        parsed = urllib.parse.urlparse(self.endpoint)
        self.host = parsed.netloc
        self.scheme = parsed.scheme or "https"
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()
        self.bucket = bucket.strip().strip("/")
        self.region = region
        self.timeout = timeout
        self._ctx = ssl.create_default_context()

    # ------------------------------------------------------------------ sign
    def _signing_key(self, date: str) -> bytes:
        k = ("AWS4" + self.secret_key).encode()
        for msg in (date, self.region, "s3", "aws4_request"):
            k = hmac.new(k, msg.encode(), hashlib.sha256).digest()
        return k

    def _signed_headers(self, method: str, path: str, query: dict, headers: dict, payload_hash: str) -> dict:
        now = _dt.datetime.now(_dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        # Only host + x-amz-* are signed; transport headers such as content-length
        # may be rewritten by the edge (R2 drops "content-length: 0" on GET).
        hdrs = {k.lower(): str(v).strip() for k, v in headers.items()}
        hdrs["host"] = self.host
        hdrs["x-amz-content-sha256"] = payload_hash
        hdrs["x-amz-date"] = amz_date
        signed = sorted(k for k in hdrs if k == "host" or k.startswith("x-amz-"))
        canonical_headers = "".join(f"{k}:{hdrs[k]}\n" for k in signed)
        canonical_query = "&".join(
            f"{_quote(k)}={_quote(str(v))}" for k, v in sorted(query.items())
        )
        canonical_request = "\n".join([
            method, path, canonical_query, canonical_headers, ";".join(signed), payload_hash,
        ])
        scope = f"{date}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        signature = hmac.new(self._signing_key(date), string_to_sign.encode(), hashlib.sha256).hexdigest()
        hdrs["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={';'.join(signed)}, Signature={signature}"
        )
        return hdrs

    # --------------------------------------------------------------- request
    def _path(self, key: str) -> str:
        key = key.lstrip("/")
        return "/" + _quote(self.bucket, "~") + ("/" + _quote(key, "/~") if key else "")

    def _request(self, method: str, key: str = "", query: Optional[dict] = None, body=None,
                 headers: Optional[dict] = None, payload_hash: str = UNSIGNED,
                 content_length: Optional[int] = None, retries: int = 4):
        query = query or {}
        headers = dict(headers or {})
        path = self._path(key)
        if content_length is not None:
            headers["content-length"] = str(content_length)
        elif isinstance(body, (bytes, bytearray)):
            headers["content-length"] = str(len(body))
        last_err = None
        for attempt in range(retries):
            try:
                signed = self._signed_headers(method, path, query, headers, payload_hash)
                conn = http.client.HTTPSConnection(self.host, timeout=self.timeout, context=self._ctx) \
                    if self.scheme == "https" else http.client.HTTPConnection(self.host, timeout=self.timeout)
                url = path + ("?" + "&".join(f"{_quote(k)}={_quote(str(v))}" for k, v in sorted(query.items())) if query else "")
                if hasattr(body, "seek"):
                    body.seek(0)
                conn.request(method, url, body=body, headers=signed)
                resp = conn.getresponse()
                data = resp.read()
                status = resp.status
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
                conn.close()
                if status in (500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if status >= 400:
                    raise R2Error(f"{method} {key or '/'} -> HTTP {status}: {data[:400].decode('utf-8', 'replace')}")
                return status, resp_headers, data
            except (OSError, http.client.HTTPException) as exc:
                last_err = exc
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise R2Error(f"{method} {key}: {exc}") from exc
        raise R2Error(f"{method} {key}: {last_err}")

    # ------------------------------------------------------------------- api
    def test(self) -> str:
        self.list(prefix="", max_keys=1, single_page=True)
        return f"ok: bucket '{self.bucket}' reachable at {self.host}"

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._request("PUT", key, body=data, headers={"content-type": content_type},
                      payload_hash=hashlib.sha256(data).hexdigest())

    def put_json(self, key: str, obj) -> None:
        import json
        self.put_bytes(key, json.dumps(obj, indent=1).encode("utf-8"), "application/json")

    def get_bytes(self, key: str) -> bytes:
        _, _, data = self._request("GET", key)
        return data

    def get_json(self, key: str):
        import json
        try:
            return json.loads(self.get_bytes(key).decode("utf-8"))
        except R2Error as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def exists(self, key: str) -> bool:
        try:
            self._request("HEAD", key)
            return True
        except R2Error as exc:
            if "HTTP 404" in str(exc):
                return False
            raise

    def delete(self, key: str) -> None:
        self._request("DELETE", key)

    def download_file(self, key: str, dest: str) -> None:
        data = self.get_bytes(key)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)

    def list(self, prefix: str, max_keys: int = 1000, single_page: bool = False) -> list[dict]:
        """Return [{'key', 'size', 'last_modified'}] for every object under prefix."""
        out: list[dict] = []
        token = None
        while True:
            q = {"list-type": "2", "prefix": prefix, "max-keys": str(max_keys)}
            if token:
                q["continuation-token"] = token
            _, _, data = self._request("GET", "", query=q)
            root = ET.fromstring(data)
            ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            for c in root.findall(f"{ns}Contents"):
                out.append({
                    "key": c.findtext(f"{ns}Key"),
                    "size": int(c.findtext(f"{ns}Size") or 0),
                    "last_modified": c.findtext(f"{ns}LastModified"),
                })
            if single_page or (root.findtext(f"{ns}IsTruncated") or "false").lower() != "true":
                break
            token = root.findtext(f"{ns}NextContinuationToken")
            if not token:
                break
        return out

    def delete_prefix(self, prefix: str) -> int:
        n = 0
        for obj in self.list(prefix):
            self.delete(obj["key"])
            n += 1
        return n

    # ------------------------------------------------------------ uploading
    def upload_file(self, path: str, key: str, progress: Optional[Callable[[int, int], None]] = None,
                    content_type: str = "application/octet-stream") -> None:
        size = os.path.getsize(path)
        if size >= MULTIPART_THRESHOLD:
            return self._upload_multipart(path, key, size, progress, content_type)
        with open(path, "rb") as fh:
            body = _ProgressReader(fh, size, progress)
            self._request("PUT", key, body=body, headers={"content-type": content_type},
                          content_length=size, retries=3)
        if progress:
            progress(size, size)

    def _upload_multipart(self, path: str, key: str, size: int, progress, content_type: str) -> None:
        _, _, data = self._request("POST", key, query={"uploads": ""}, headers={"content-type": content_type})
        root = ET.fromstring(data)
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        upload_id = root.findtext(f"{ns}UploadId")
        if not upload_id:
            raise R2Error("multipart init returned no UploadId")
        parts = []
        sent = 0
        try:
            with open(path, "rb") as fh:
                part_no = 1
                while True:
                    chunk = fh.read(PART_SIZE)
                    if not chunk:
                        break
                    _, hdrs, _ = self._request(
                        "PUT", key, query={"partNumber": str(part_no), "uploadId": upload_id},
                        body=chunk, payload_hash=hashlib.sha256(chunk).hexdigest(), retries=5,
                    )
                    etag = hdrs.get("etag", "").strip()
                    if not etag:
                        raise R2Error(f"part {part_no} returned no ETag")
                    parts.append((part_no, etag))
                    sent += len(chunk)
                    if progress:
                        progress(sent, size)
                    part_no += 1
            body = "<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{n}</PartNumber><ETag>{et}</ETag></Part>" for n, et in parts
            ) + "</CompleteMultipartUpload>"
            data = body.encode()
            self._request("POST", key, query={"uploadId": upload_id}, body=data,
                          headers={"content-type": "application/xml"},
                          payload_hash=hashlib.sha256(data).hexdigest())
        except Exception:
            try:
                self._request("DELETE", key, query={"uploadId": upload_id})
            except Exception:
                pass
            raise


class _ProgressReader(io.RawIOBase):
    """File wrapper that reports bytes read so uploads can show progress."""

    def __init__(self, fh, size: int, cb):
        self.fh, self.size, self.cb, self.sent = fh, size, cb, 0

    def readable(self):
        return True

    def read(self, n=-1):
        data = self.fh.read(n)
        self.sent += len(data)
        if self.cb and data:
            self.cb(self.sent, self.size)
        return data

    def seek(self, pos, whence=0):
        self.sent = 0
        return self.fh.seek(pos, whence)

    def __len__(self):
        return self.size
