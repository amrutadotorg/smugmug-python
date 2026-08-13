"""SmugMug API v2 client.

Standalone client library (fork of the amruta.org SmugMug automation code).
No dependency on host applications — credentials are passed explicitly and
logging goes through the standard ``logging`` module (logger name: "smugmug").
"""

import hashlib
import logging
import mimetypes
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import batched
from pathlib import Path
from typing import Any

import requests
from requests_oauthlib import OAuth1Session
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
)

logger = logging.getLogger("smugmug")

API_BASE = "https://api.smugmug.com"
UPLOAD_URL = "https://upload.smugmug.com/"
REQUEST_TIMEOUT = 30
UPLOAD_TIMEOUT = 120
CHUNK_SIZE = 500
ILLEGAL_PATH_CHARS = re.compile(r'[\/\\:*?"<>|]')


def _sanitize_path_segment(name: str) -> str:
    return ILLEGAL_PATH_CHARS.sub("-", name)


def _split_album_path(full_path: str) -> tuple[str, str]:
    """Split "parent/album" path into folder and album names, sanitizing each segment.

    Sanitization happens per segment so the "/" separator survives (replacing it
    inside the whole path would collapse the hierarchy into a single name).
    """
    parts = [_sanitize_path_segment(p).strip() for p in str(full_path).split("/")]
    parent_name = parts[0]
    album_name = parts[1] if len(parts) > 1 else parts[0]
    return parent_name, album_name


class SmugMugError(Exception):
    pass


class RateLimitError(SmugMugError):
    """Raised when SmugMug returns HTTP 429. Carries the Retry-After hint."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


RATE_LIMIT_WARNING_THRESHOLD = 10
RATE_LIMIT_MAX_SLEEP = 60.0


def _parse_retry_after(r) -> float | None:
    """Parse the Retry-After header (seconds per SmugMug docs)."""
    raw = r.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except TypeError, ValueError:
        return None


def _rate_limit_error(r, context: str) -> RateLimitError:
    return RateLimitError(
        f"{context}: HTTP 429 — {r.text[:200]}", retry_after=_parse_retry_after(r)
    )


def _log_rate_limit_headers(r) -> None:
    """Warn when the current window is nearly exhausted (X-RateLimit-Remaining)."""
    remaining = r.headers.get("X-RateLimit-Remaining")
    if (
        remaining
        and remaining.isdigit()
        and int(remaining) <= RATE_LIMIT_WARNING_THRESHOLD
    ):
        logger.warning(
            f"SmugMug rate limit low: {remaining} requests remaining in window"
        )


def _wait_after_rate_limit(retry_state) -> float:
    """Sleep Retry-After on 429 (capped), exponential backoff otherwise."""
    exc = retry_state.outcome.exception()
    if isinstance(exc, RateLimitError) and exc.retry_after:
        return min(max(exc.retry_after, 1.0), RATE_LIMIT_MAX_SLEEP)
    return min(2**retry_state.attempt_number, 10.0)


_SESSION_RETRY = retry(
    retry=retry_if_exception_type(
        (
            RateLimitError,
            SmugMugError,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        )
    ),
    wait=_wait_after_rate_limit,
    stop=stop_after_attempt(5),
    reraise=True,
)


class SmugMugClient:
    def __init__(self, session: OAuth1Session, root_node_uri: str):
        self.session = session
        self.root_node_uri = root_node_uri
        self.headers = {"Accept": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.close()
        return False

    @classmethod
    def from_config(
        cls,
        api_key: str,
        api_secret: str,
        access_token: str,
        token_secret: str,
        nickname: str | None = None,
    ) -> SmugMugClient:
        """Authenticate and return a client.

        Args:
            api_key: SmugMug API key.
            api_secret: SmugMug API secret.
            access_token: OAuth1 resource owner key.
            token_secret: OAuth1 resource owner secret.
            nickname: Expected account nickname (used only for logging).
        """
        if not api_key or not api_secret:
            raise SmugMugError("SmugMug API key/secret not configured")
        if not access_token or not token_secret:
            raise SmugMugError(
                "SmugMug access token not configured — run OOB PIN flow first"
            )

        session = OAuth1Session(
            api_key,
            client_secret=api_secret,
            resource_owner_key=access_token,
            resource_owner_secret=token_secret,
        )

        r = session.get(
            f"{API_BASE}/api/v2!authuser",
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            session.close()
            raise SmugMugError(f"Auth failed: HTTP {r.status_code} — {r.text[:200]}")
        authuser = r.json()
        root_node_uri = authuser["Response"]["User"]["Uris"]["Node"]["Uri"]
        logged_in = authuser["Response"]["User"].get("NickName", "?")
        if nickname and logged_in != nickname:
            logger.warning(f"Authenticated as {logged_in}, expected {nickname}")
        logger.debug(f"SmugMugClient: logged in as {logged_in}")
        return cls(session, root_node_uri)

    @_SESSION_RETRY
    def _get(self, uri: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self.session.get(
            f"{API_BASE}{uri}",
            headers=self.headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 429:
            raise _rate_limit_error(r, f"GET {uri}")
        if r.status_code not in (200, 201):
            raise SmugMugError(f"GET {uri}: HTTP {r.status_code} — {r.text[:300]}")
        _log_rate_limit_headers(r)
        return r.json()

    @_SESSION_RETRY
    def _post(
        self,
        uri: str,
        data: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"headers": self.headers, "timeout": REQUEST_TIMEOUT}
        if json_payload:
            kwargs["json"] = json_payload
        elif data:
            kwargs["data"] = data
        r = self.session.post(f"{API_BASE}{uri}", **kwargs)  # type: ignore[arg-type]
        if r.status_code == 429:
            raise _rate_limit_error(r, f"POST {uri}")
        if r.status_code not in (200, 201):
            raise SmugMugError(f"POST {uri}: HTTP {r.status_code} — {r.text[:300]}")
        _log_rate_limit_headers(r)
        return r.json()

    @_SESSION_RETRY
    def _patch(self, uri: str, payload: dict[str, Any]) -> bool:
        r = self.session.patch(
            f"{API_BASE}{uri}",
            headers=self.headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 429:
            raise _rate_limit_error(r, f"PATCH {uri}")
        if r.status_code == 200:
            _log_rate_limit_headers(r)
            return True
        logger.warning(f"PATCH {uri}: HTTP {r.status_code} — {r.text[:200]}")
        return False

    @_SESSION_RETRY
    def _delete(self, uri: str) -> bool:
        r = self.session.delete(f"{API_BASE}{uri}", timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            raise _rate_limit_error(r, f"DELETE {uri}")
        _log_rate_limit_headers(r)
        return r.status_code in (200, 201)

    # --- Node operations ---

    def get_node_children(
        self, node_uri: str, count: int = 500
    ) -> list[dict[str, Any]]:
        """List children of a node. Propagates errors — callers must handle SmugMugError."""
        data = self._get(f"{node_uri}!children", {"count": count})
        return data.get("Response", {}).get("Node", [])

    def get_node(self, node_uri: str) -> dict[str, Any] | None:
        try:
            data = self._get(node_uri)
            return data.get("Response", {}).get("Node")
        except SmugMugError:
            return None

    def get_album_info(self, album_uri: str) -> dict[str, Any] | None:
        try:
            data = self._get(album_uri)
            return data.get("Response", {}).get("Album")
        except SmugMugError:
            return None

    def get_or_create_node(
        self,
        parent_node_uri: str,
        node_type: str,
        node_name: str,
        node_password: str | None = None,
    ) -> str:
        try:
            children = self.get_node_children(parent_node_uri)
        except SmugMugError:
            logger.error(
                f"Cannot list children of {parent_node_uri} — refusing to create '{node_name}' to prevent duplicates"
            )
            raise

        for child in children:
            if child.get("Name") == node_name:
                logger.debug(f'Node "{node_name}" ({node_type}) exists: {child["Uri"]}')
                return child["Uri"]

        logger.info(f'Creating {node_type} "{node_name}" under {parent_node_uri}')
        payload: dict[str, Any] = {
            "Type": node_type,
            "Name": node_name,
            "Privacy": "Public",
        }
        if node_type == "Folder":
            payload["SortMethod"] = 3
            payload["SortDirection"] = 1
        if node_password:
            payload["SecurityType"] = "Password"
            payload["Password"] = node_password

        try:
            result = self._post(f"{parent_node_uri}!children", data=payload)
            new_uri = result["Response"]["Node"]["Uri"]
            logger.info(f'Created {node_type} "{node_name}" → {new_uri}')
            return new_uri
        except SmugMugError as e:
            if "Conflict" in str(e) or "409" in str(e):
                try:
                    r = self.session.get(
                        f"{API_BASE}{parent_node_uri}!children",
                        headers=self.headers,
                        params={"count": 100},
                        timeout=REQUEST_TIMEOUT,
                    )
                    for child in r.json().get("Response", {}).get("Node", []):
                        if child.get("Name") == node_name:
                            return child["Uri"]
                except (
                    SmugMugError,
                    requests.exceptions.RequestException,
                    requests.exceptions.Timeout,
                ) as e:
                    logger.warning(
                        f"Conflict resolution retry failed for '{node_name}': {e}"
                    )
            raise

    def get_album_uri(self, node_uri: str) -> str:
        node = self._get(node_uri)
        return node["Response"]["Node"]["Uris"]["Album"]["Uri"]

    def get_weburi(self, node_uri: str) -> str | None:
        node = self.get_node(node_uri)
        return node["WebUri"] if node else None

    def image_count(self, album_uri: str) -> int:
        data = self._get(f"{album_uri}", params={"_filter": "ImageCount"})
        return data["Response"]["Album"]["ImageCount"]

    def get_album_images(self, album_uri: str) -> list[str]:
        uris: list[str] = []
        next_page_uri: str | None = f"{album_uri}!images"
        while next_page_uri:
            data = self._get(next_page_uri, {"count": 500})
            uris.extend(
                x["Uri"] for x in data.get("Response", {}).get("AlbumImage", [])
            )
            next_page_uri = data.get("Response", {}).get("Pages", {}).get("NextPage")
        return uris

    # --- Album organization ---

    def move_images(self, from_album_uri: str, to_album_uri: str) -> bool:
        uris = self.get_album_images(from_album_uri)
        if not uris:
            logger.debug(f"No images to move from {from_album_uri}")
            return True
        return self._move_collect_chunked(to_album_uri, uris, "moveimages")

    def collect_images(self, from_album_uri: str, to_album_uri: str) -> bool:
        uris = self.get_album_images(from_album_uri)
        if not uris:
            return True
        return self._move_collect_chunked(to_album_uri, uris, "collectimages")

    def collect_image_uris(self, image_uris: list[str], to_album_uri: str) -> bool:
        if not image_uris:
            return True
        return self._move_collect_chunked(to_album_uri, image_uris, "collectimages")

    def move_image_uris(self, image_uris: list[str], to_album_uri: str) -> bool:
        if not image_uris:
            return True
        return self._move_collect_chunked(to_album_uri, image_uris, "moveimages")

    def _move_collect_chunked(
        self, to_album_uri: str, uris: list[str], endpoint: str
    ) -> bool:
        key = "MoveUris" if endpoint == "moveimages" else "CollectUris"
        all_ok = True
        for i, chunk in enumerate(batched(uris, CHUNK_SIZE, strict=False), 1):
            try:
                resp = self._post(
                    f"{to_album_uri}!{endpoint}",
                    json_payload={"Async": True, key: ",".join(chunk)},
                )
                logger.info(
                    f"{endpoint} chunk {i}: {len(chunk)} images → {to_album_uri}"
                )
                self._poll_async_job(resp)
            except SmugMugError as e:
                logger.error(f"{endpoint} chunk failed: {e}")
                all_ok = False
            time.sleep(0.5)
        return all_ok

    def _poll_async_job(
        self,
        response: dict[str, Any],
        poll_interval: float = 2.0,
        max_wait: float = 120.0,
    ) -> None:
        job_uri = response.get("Response", {}).get("Uri", "")
        if not job_uri:
            return
        elapsed = 0.0
        while elapsed < max_wait:
            try:
                status_resp = self._get(job_uri)
                status = status_resp.get("Response", {}).get("Status", "")
                if status == "Completed":
                    logger.debug(f"Async job {job_uri} completed in {elapsed:.0f}s")
                    return
                if status == "Failed":
                    logger.error(f"Async job {job_uri} failed")
                    return
            except SmugMugError as e:
                logger.warning(f"Async job poll failed for {job_uri}: {e}")
            time.sleep(poll_interval)
            elapsed += poll_interval
        logger.warning(f"Async job {job_uri} did not complete within {max_wait}s")

    def delete_album(self, album_uri: str) -> bool:
        ok = self._delete(album_uri)
        if ok:
            logger.info(f"Deleted album {album_uri}")
        else:
            logger.warning(f"Failed to delete {album_uri}")
        return ok

    def patch_album_description(self, node_uri: str, description: str) -> bool:
        return self._patch(node_uri, {"Description": description})

    # --- Deduplication ---

    def get_album_image_hashes(self, album_uri: str) -> list[dict[str, str]]:
        images: list[dict[str, str]] = []
        next_page_uri: str | None = f"{album_uri}!images"
        while next_page_uri:
            data = self._get(next_page_uri, {"count": 500, "_expand": "Image"})
            for img in data.get("Response", {}).get("AlbumImage", []):
                images.append(
                    {
                        "FileName": img["FileName"],
                        "ArchivedMD5": img["ArchivedMD5"],
                        "Uri": img.get("Uri", ""),
                        "ImageKey": img.get("ImageKey", ""),
                    }
                )
            next_page_uri = data.get("Response", {}).get("Pages", {}).get("NextPage")
        return images

    def upload_new_only(
        self, album_uri: str, folder_path: Path
    ) -> tuple[list[dict[str, str]], int]:
        try:
            existing_hashes = self.get_album_image_hashes(album_uri)
        except (SmugMugError, requests.exceptions.RequestException) as e:
            logger.error(
                f"Cannot fetch existing images for dedup, aborting upload: {e}"
            )
            return [], 0

        existing_md5s = {h["ArchivedMD5"] for h in existing_hashes}
        existing_names = {h["FileName"] for h in existing_hashes}

        all_files = sorted(
            [
                f
                for f in folder_path.iterdir()
                if f.is_file() and not f.name.startswith(".")
            ],
            key=lambda x: x.name.lower(),
        )
        if not all_files:
            return [], 0

        uploaded: list[dict[str, str]] = []
        skipped = 0
        for fp in all_files:
            image_data = fp.read_bytes()
            local_md5 = hashlib.md5(image_data).hexdigest()
            if local_md5 in existing_md5s:
                logger.debug(f"Dup skip: {fp.name} (MD5: {local_md5[:8]}...)")
                skipped += 1
                continue
            if fp.name in existing_names:
                logger.warning(
                    f"Filename exists but MD5 differs: {fp.name} (will re-upload)"
                )
            result = self._upload_bytes(album_uri, fp.name, image_data)
            if result:
                result["file_name"] = fp.name
                uploaded.append(result)
            time.sleep(0.2)

        logger.info(
            f"Uploaded {len(uploaded)} new, skipped {skipped} duplicates from {folder_path.name}"
        )
        return uploaded, skipped

    # --- Upload ---

    def upload_file(self, album_uri: str, file_path: Path) -> dict[str, str] | None:
        with open(file_path, "rb") as f:
            image_data = f.read()
        return self._upload_bytes(album_uri, file_path.name, image_data)

    def _upload_bytes(
        self, album_uri: str, file_name: str, image_data: bytes
    ) -> dict[str, str] | None:
        mime_type, _ = mimetypes.guess_type(file_name)
        content_type = mime_type or "image/jpeg"

        md5_hash = hashlib.md5(image_data).hexdigest()
        headers = {
            "Accept": "application/json",
            "Content-Length": str(len(image_data)),
            "Content-MD5": md5_hash,
            "Content-Type": content_type,
            "X-Smug-AlbumUri": album_uri,
            "X-Smug-FileName": file_name,
            "X-Smug-ResponseType": "JSON",
            "X-Smug-Version": "v2",
        }

        @retry(
            retry=retry_if_exception_type(
                (
                    RateLimitError,
                    SmugMugError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                )
            ),
            wait=_wait_after_rate_limit,
            stop=stop_after_attempt(4),
            reraise=True,
            after=lambda retry_state: (
                logger.warning(
                    f"Upload {file_name}: {retry_state.outcome.exception()}, retry {retry_state.attempt_number}/3"
                )
                if retry_state.outcome and retry_state.outcome.failed
                else None
            ),
        )
        def _do_upload():
            r = self.session.post(
                UPLOAD_URL, headers=headers, data=image_data, timeout=UPLOAD_TIMEOUT
            )
            if r.status_code == 429:
                raise _rate_limit_error(r, f"Upload {file_name}")
            if r.status_code == 200:
                result = r.json()
                if result.get("stat") == "ok":
                    return result
            if 500 <= r.status_code < 600:
                raise SmugMugError(f"HTTP {r.status_code} — {r.text[:200]}")
            raise SmugMugError(f"HTTP {r.status_code}: {r.text[:200]}")

        try:
            result = _do_upload()
        except SmugMugError as e:
            logger.error(f"Upload FAILED for {file_name}: {e}")
            return None

        img = result.get("Image", {})
        image_url = img.get("URL", "")
        image_uri = img.get("ImageUri", "")
        upload_key = img.get("UploadKey", "")
        logger.info(f"Upload OK: {file_name} → {image_url}")
        return {
            "url": image_url,
            "image_uri": image_uri,
            "image_key": upload_key,
            "md5": md5_hash,
            "file_name": file_name,
        }

    def upload_folder(
        self, album_uri: str, folder_path: Path, max_workers: int = 4
    ) -> list[dict[str, str]]:
        files = sorted(
            [
                f
                for f in folder_path.iterdir()
                if f.is_file() and not f.name.startswith(".")
            ],
            key=lambda x: x.name.lower(),
        )
        if not files:
            logger.warning(f"No files in {folder_path}")
            return []

        logger.info(
            f"Uploading {len(files)} files from {folder_path.name} ({max_workers} workers)"
        )
        uploaded: list[dict[str, str]] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self.upload_file, album_uri, fp): fp.name
                for fp in files
            }
            for future in as_completed(future_map):
                fname = future_map[future]
                try:
                    result = future.result()
                    if result:
                        uploaded.append(result)
                except (SmugMugError, requests.exceptions.RequestException) as e:
                    logger.error(f"Upload thread failed for {fname}: {e}")

        logger.info(
            f"Uploaded {len(uploaded)}/{len(files)} files from {folder_path.name}"
        )
        return uploaded

    # --- Bulk operations ---

    def delete_empty_albums(self, parent_node_uri: str) -> int:
        deleted = 0
        for child in self.get_node_children(parent_node_uri):
            if child.get("Type") != "Album":
                continue
            album_uri = child["Uris"]["Album"]["Uri"]
            try:
                cnt = self.image_count(album_uri)
            except SmugMugError:
                logger.warning(
                    f"Cannot check image count for {album_uri}, skipping delete"
                )
                continue
            if cnt == 0:
                if self.delete_album(album_uri):
                    deleted += 1
                time.sleep(0.3)
        return deleted

    def get_album_else_create(self, parent_node_uri: str, full_path: str) -> str:
        parent_name, album_name = _split_album_path(full_path)
        node_uri = self.get_or_create_node(parent_node_uri, "Folder", parent_name)
        album_node_uri = self.get_or_create_node(node_uri, "Album", album_name)
        return self.get_album_uri(album_node_uri)
