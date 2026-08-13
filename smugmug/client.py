"""SmugMug API v2 client.

Standalone client library (fork of the amruta.org SmugMug automation code).
No dependency on host applications — credentials are passed explicitly and
logging goes through the standard ``logging`` module (logger name: "smugmug").
"""

import functools
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
    retry_if_exception,
    stop_after_attempt,
)

logger = logging.getLogger("smugmug")

API_BASE = "https://api.smugmug.com"
UPLOAD_URL = "https://upload.smugmug.com/"
REQUEST_TIMEOUT = 30
UPLOAD_TIMEOUT = 120
CHUNK_SIZE = 500
ILLEGAL_PATH_CHARS = re.compile(r'[\/\\:*?"<>|]')

# Folder sort settings — values validated empirically against production
# accounts (SortMethod/SortDirection enums of the API v2 docs). Do not "fix"
# these without checking the live account: they control folder child
# ordering and were verified against real folders, not the docs.
FOLDER_SORT_METHOD = 3
FOLDER_SORT_DIRECTION = 1

# Pause between sequential uploads (gentle on rate limits).
_SEQUENTIAL_UPLOAD_PACE = 0.2

# MIME types mimetypes.guess_type() does not know (RAW camera formats).
_RAW_MIME_TYPES = {
    ".arw": "image/x-sony-arw",
    ".dng": "image/x-adobe-dng",
    ".rw2": "image/x-panasonic-rw2",
}


def _sanitize_path_segment(name: str) -> str:
    return ILLEGAL_PATH_CHARS.sub("-", name)


def _split_album_path(full_path: str) -> tuple[str, str]:
    """Split "parent/album" path into folder and album names, sanitizing each segment.

    Sanitization happens per segment so the "/" separator survives (replacing it
    inside the whole path would collapse the hierarchy into a single name).
    Leading/trailing slashes are ignored so "Parent/Album/", "/Parent/Album" and
    "/Album/" work like their slash-less counterparts.
    """
    full_path = str(full_path).strip("/")
    parts = [_sanitize_path_segment(p).strip() for p in full_path.split("/")]
    if len(parts) > 2:
        raise SmugMugError(
            f"Expected 'parent/album' path, got {len(parts)} segments: {full_path!r}"
        )
    parent_name = parts[0]
    album_name = parts[1] if len(parts) > 1 else parts[0]
    return parent_name, album_name


def _list_upload_files(
    folder_path: Path, extensions: set[str] | None = None
) -> list[Path]:
    """List uploadable files in a folder, sorted by name (optionally filtered)."""
    files = [
        f for f in folder_path.iterdir() if f.is_file() and not f.name.startswith(".")
    ]
    if extensions:
        files = [f for f in files if f.suffix.lower() in extensions]
    return sorted(files, key=lambda x: x.name.lower())


def _album_uri_from_node(node: dict[str, Any], node_uri: str) -> str:
    """Extract the album URI a node points at, or fail loudly if it is not an album."""
    album_uri = node.get("Uris", {}).get("Album", {}).get("Uri")
    if not album_uri:
        raise SmugMugError(f"Node {node_uri} is not an album (no Album.Uri)")
    return album_uri


def _filter_duplicates(
    files: list[Path],
    existing_md5s: set[str],
    existing_names: set[str],
) -> tuple[list[Path], int]:
    """Split files into (to_upload, skipped) by MD5 against the album's images."""
    kept: list[Path] = []
    skipped = 0
    for fp in files:
        local_md5 = hashlib.md5(fp.read_bytes()).hexdigest()
        if local_md5 in existing_md5s:
            logger.debug(f"Dup skip: {fp.name} (MD5: {local_md5[:8]}...)")
            skipped += 1
            continue
        if fp.name in existing_names:
            logger.warning(
                f"Filename exists but MD5 differs: {fp.name} (will re-upload)"
            )
        kept.append(fp)
    return kept, skipped


class SmugMugError(Exception):
    """Base error. Optionally carries the HTTP status code that caused it."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


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
    # fmt: off
    # Parenthesized on purpose: PEP 758 dropped the parens only without "as";
    # adding "as e" would silently break the unparenthesized form. ruff's
    # formatter strips the parens for 3.14 targets, hence the fmt directives.
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
    # fmt: on


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


def _is_retryable(exc: BaseException) -> bool:
    """True only for errors a retry could plausibly fix.

    Rate limits and network errors yes; 4xx client errors (bad token, not
    found, conflict) would fail identically on every attempt, so retrying
    them only adds latency. Unknown status codes are treated as fatal.
    """
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(
        exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
    ):
        return True
    return (
        isinstance(exc, SmugMugError)
        and exc.status_code is not None
        and exc.status_code >= 500
    )


_SESSION_RETRY = retry(
    retry=retry_if_exception(_is_retryable),
    wait=_wait_after_rate_limit,
    stop=stop_after_attempt(5),
    reraise=True,
)

_UPLOAD_MAX_ATTEMPTS = 4


def _wrap_network_errors(verb: str):
    """Convert network errors to SmugMugError AFTER retries are exhausted.

    Must wrap OUTSIDE the retry decorator: tenacity's predicate needs the raw
    Timeout/ConnectionError to decide to retry; once reraise=True re-raises
    it, callers get a single error type instead of leaking requests exceptions.
    """

    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(self, uri: str, *args, **kwargs):
            try:
                return fn(self, uri, *args, **kwargs)
            except requests.exceptions.RequestException as e:
                raise SmugMugError(
                    f"{verb} {uri}: network error after retries — {e}"
                ) from e

        return wrapped

    return decorate


def _make_upload_retry(file_name: str):
    """Upload-specific retry: same predicate as _SESSION_RETRY, logged per file."""

    def _log_retry(retry_state) -> None:
        if retry_state.outcome and retry_state.outcome.failed:
            logger.warning(
                f"Upload {file_name}: {retry_state.outcome.exception()}, "
                f"retry {retry_state.attempt_number - 1}/{_UPLOAD_MAX_ATTEMPTS - 1}"
            )

    return retry(
        retry=retry_if_exception(_is_retryable),
        wait=_wait_after_rate_limit,
        stop=stop_after_attempt(_UPLOAD_MAX_ATTEMPTS),
        reraise=True,
        after=_log_retry,
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
        # OAuth1Session signs via the Authorization header by default — the
        # SmugMug uploader only accepts OAuth params in the header, never in
        # the query string or body. Keep the default.
        try:
            r = session.get(
                f"{API_BASE}/api/v2!authuser",
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            session.close()
            raise SmugMugError(f"Auth request failed: {e}") from e
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

    @_wrap_network_errors("GET")
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
            raise SmugMugError(
                f"GET {uri}: HTTP {r.status_code} — {r.text[:300]}",
                status_code=r.status_code,
            )
        _log_rate_limit_headers(r)
        # Unwrap the "Response" envelope once here so callers never have to
        # know the transport shape (fixing this in one place if it changes).
        return r.json().get("Response", {})

    @_wrap_network_errors("POST")
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
            raise SmugMugError(
                f"POST {uri}: HTTP {r.status_code} — {r.text[:300]}",
                status_code=r.status_code,
            )
        _log_rate_limit_headers(r)
        return r.json().get("Response", {})

    @_wrap_network_errors("PATCH")
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
        if 500 <= r.status_code < 600:
            raise SmugMugError(
                f"PATCH {uri}: HTTP {r.status_code} — {r.text[:200]}",
                status_code=r.status_code,
            )
        logger.warning(f"PATCH {uri}: HTTP {r.status_code} — {r.text[:200]}")
        return False

    @_wrap_network_errors("DELETE")
    @_SESSION_RETRY
    def _delete(self, uri: str) -> bool:
        r = self.session.delete(f"{API_BASE}{uri}", timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            raise _rate_limit_error(r, f"DELETE {uri}")
        if 500 <= r.status_code < 600:
            raise SmugMugError(
                f"DELETE {uri}: HTTP {r.status_code} — {r.text[:200]}",
                status_code=r.status_code,
            )
        _log_rate_limit_headers(r)
        return r.status_code in (200, 201, 204)

    # --- Node operations ---

    def get_node_children(
        self, node_uri: str, count: int = 500
    ) -> list[dict[str, Any]]:
        """List all children of a node (paginated). Propagates errors — callers must handle SmugMugError."""
        children: list[dict[str, Any]] = []
        next_page_uri: str | None = f"{node_uri}!children"
        while next_page_uri:
            data = self._get(next_page_uri, {"count": count})
            children.extend(data.get("Node", []))
            next_page_uri = data.get("Pages", {}).get("NextPage")
        return children

    def get_node(
        self, node_uri: str, raise_on_error: bool = False
    ) -> dict[str, Any] | None:
        try:
            data = self._get(node_uri)
            return data.get("Node")
        except SmugMugError:
            if raise_on_error:
                raise
            return None

    def get_album_info(
        self, album_uri: str, raise_on_error: bool = False
    ) -> dict[str, Any] | None:
        try:
            data = self._get(album_uri)
            return data.get("Album")
        except SmugMugError:
            if raise_on_error:
                raise
            return None

    def get_or_create_node(
        self,
        parent_node_uri: str,
        node_type: str,
        node_name: str,
        node_password: str | None = None,
        privacy: str = "Public",
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
        # UrlName intentionally omitted — SmugMug derives the URL slug from
        # Name; sending our own would add a collision/sanitization surface
        # (409s on illegal slug characters).
        payload: dict[str, Any] = {
            "Type": node_type,
            "Name": node_name,
            "Privacy": privacy,
        }
        if node_type == "Folder":
            payload["SortMethod"] = FOLDER_SORT_METHOD
            payload["SortDirection"] = FOLDER_SORT_DIRECTION
        if node_password:
            payload["SecurityType"] = "Password"
            payload["Password"] = node_password

        try:
            result = self._post(f"{parent_node_uri}!children", data=payload)
            new_uri = result["Node"]["Uri"]
            logger.info(f'Created {node_type} "{node_name}" → {new_uri}')
            return new_uri
        except SmugMugError as e:
            if e.status_code == 409:
                try:
                    for child in self.get_node_children(parent_node_uri):
                        if child.get("Name") == node_name:
                            return child["Uri"]
                except (
                    SmugMugError,
                    requests.exceptions.RequestException,
                ) as conflict_e:
                    logger.warning(
                        f"Conflict resolution retry failed for '{node_name}': {conflict_e}"
                    )
            raise

    def get_album_uri(self, node_uri: str) -> str:
        node = self._get(node_uri).get("Node", {})
        return _album_uri_from_node(node, node_uri)

    def get_weburi(self, node_uri: str) -> str | None:
        node = self.get_node(node_uri)
        return node.get("WebUri") if node else None

    def image_count(self, album_uri: str) -> int:
        data = self._get(album_uri, params={"_filter": "ImageCount"})
        count = data.get("Album", {}).get("ImageCount")
        if count is None:
            raise SmugMugError(f"Album {album_uri} has no ImageCount")
        return count

    def get_album_images(self, album_uri: str) -> list[str]:
        uris: list[str] = []
        next_page_uri: str | None = f"{album_uri}!images"
        while next_page_uri:
            data = self._get(next_page_uri, {"count": 500})
            uris.extend(x["Uri"] for x in data.get("AlbumImage", []))
            next_page_uri = data.get("Pages", {}).get("NextPage")
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
        chunks = list(batched(uris, CHUNK_SIZE, strict=False))
        all_ok = True
        for i, chunk in enumerate(chunks, 1):
            try:
                resp = self._post(
                    f"{to_album_uri}!{endpoint}",
                    json_payload={"Async": True, key: ",".join(chunk)},
                )
                logger.info(
                    f"{endpoint} chunk {i}: {len(chunk)} images → {to_album_uri}"
                )
                if not self._poll_async_job(resp):
                    all_ok = False
            except SmugMugError as e:
                logger.error(f"{endpoint} chunk failed: {e}")
                all_ok = False
            if i < len(chunks):
                time.sleep(0.5)
        return all_ok

    def _poll_async_job(
        self,
        response: dict[str, Any],
        poll_interval: float = 2.0,
        max_wait: float = 120.0,
    ) -> bool:
        """Poll an async job until it completes. True only on "Completed"."""
        job_uri = response.get("Uri", "")
        if not job_uri:
            logger.warning("Async job response has no Uri to poll")
            return False
        elapsed = 0.0
        while elapsed < max_wait:
            try:
                status_resp = self._get(job_uri)
                status = status_resp.get("Status", "")
                if status == "Completed":
                    logger.debug(f"Async job {job_uri} completed in {elapsed:.0f}s")
                    return True
                if status == "Failed":
                    logger.error(f"Async job {job_uri} failed")
                    return False
            except SmugMugError as e:
                logger.warning(f"Async job poll failed for {job_uri}: {e}")
            time.sleep(poll_interval)
            elapsed += poll_interval
        logger.warning(f"Async job {job_uri} did not complete within {max_wait}s")
        return False

    def delete_album(self, album_uri: str) -> bool:
        try:
            ok = self._delete(album_uri)
        except SmugMugError as e:
            logger.warning(f"Failed to delete {album_uri}: {e}")
            return False
        if ok:
            logger.info(f"Deleted album {album_uri}")
        else:
            logger.warning(f"Failed to delete {album_uri}")
        return ok

    def patch_album_description(self, node_uri: str, description: str) -> bool:
        try:
            return self._patch(node_uri, {"Description": description})
        except SmugMugError as e:
            logger.warning(f"Failed to patch description of {node_uri}: {e}")
            return False

    # --- Deduplication ---

    def get_album_image_hashes(self, album_uri: str) -> list[dict[str, str]]:
        images: list[dict[str, str]] = []
        next_page_uri: str | None = f"{album_uri}!images"
        while next_page_uri:
            data = self._get(next_page_uri, {"count": 500, "_expand": "Image"})
            for img in data.get("AlbumImage", []):
                images.append(
                    {
                        "FileName": img.get("FileName", ""),
                        "ArchivedMD5": img.get("ArchivedMD5", ""),
                        "Uri": img.get("Uri", ""),
                        "ImageKey": img.get("ImageKey", ""),
                    }
                )
            next_page_uri = data.get("Pages", {}).get("NextPage")
        return images

    def upload(
        self,
        album_uri: str,
        folder_path: Path,
        *,
        dedup: bool = False,
        max_workers: int = 1,
        extensions: set[str] | None = None,
    ) -> tuple[list[dict[str, str]], int]:
        """Upload files from a folder, optionally skipping duplicates.

        ``dedup`` filters out files whose MD5 already exists in the album
        (a pre-pass, before anything is uploaded). ``max_workers=1`` uploads
        sequentially with a small pause; higher values upload in parallel.
        Returns (uploaded, skipped).

        The whole file is loaded into memory for hashing and upload — fine
        for typical JPEGs, but budget RAM when uploading large video/RAW
        files, especially in parallel.
        """
        files = _list_upload_files(folder_path, extensions)
        if not files:
            return [], 0

        skipped = 0
        if dedup:
            try:
                existing_hashes = self.get_album_image_hashes(album_uri)
            except (SmugMugError, requests.exceptions.RequestException) as e:
                logger.error(
                    f"Cannot fetch existing images for dedup, aborting upload: {e}"
                )
                return [], 0
            existing_md5s = {h["ArchivedMD5"] for h in existing_hashes}
            existing_names = {h["FileName"] for h in existing_hashes}
            files, skipped = _filter_duplicates(files, existing_md5s, existing_names)

        if max_workers == 1:
            uploaded = self._upload_sequential(album_uri, files)
        else:
            uploaded = self._upload_parallel(album_uri, files, max_workers)

        logger.info(
            f"Uploaded {len(uploaded)}/{len(files)} files, skipped {skipped}, "
            f"failed {len(files) - len(uploaded)} from {folder_path.name}"
        )
        return uploaded, skipped

    def _upload_sequential(
        self, album_uri: str, files: list[Path]
    ) -> list[dict[str, str]]:
        uploaded: list[dict[str, str]] = []
        for fp in files:
            result = self.upload_file(album_uri, fp)
            if result:
                uploaded.append(result)
            time.sleep(_SEQUENTIAL_UPLOAD_PACE)
        return uploaded

    # --- Upload ---

    def upload_file(self, album_uri: str, file_path: Path) -> dict[str, str] | None:
        """Upload a single file. Loads it fully into memory (see upload_new_only)."""
        with open(file_path, "rb") as f:
            image_data = f.read()
        return self._upload_bytes(album_uri, file_path.name, image_data)

    def _upload_bytes(
        self, album_uri: str, file_name: str, image_data: bytes
    ) -> dict[str, str] | None:
        # Control characters would be rejected by requests (InvalidHeader);
        # fail with a clear SmugMugError before the request instead.
        if re.search(r"[\x00-\x1f\x7f]", file_name):
            raise SmugMugError(f"File name contains control characters: {file_name!r}")
        mime_type, _ = mimetypes.guess_type(file_name)
        content_type = mime_type or _RAW_MIME_TYPES.get(Path(file_name).suffix.lower())
        if not content_type:
            logger.warning(
                f"Unknown file type for {file_name!r}, sending as image/jpeg"
            )
            content_type = "image/jpeg"

        # Content-MD5 is sent as hex: SmugMug compares it as an opaque dedup
        # key and reports the same 32-char hex digest as ArchivedMD5 (RFC 1864
        # base64 is NOT what the live API accepts — see community clients).
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

        @_make_upload_retry(file_name)
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
                raise SmugMugError(
                    f"HTTP {r.status_code} — {r.text[:200]}",
                    status_code=r.status_code,
                )
            raise SmugMugError(
                f"HTTP {r.status_code}: {r.text[:200]}", status_code=r.status_code
            )

        try:
            result = _do_upload()
        except (SmugMugError, requests.exceptions.RequestException) as e:
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

    def upload_new_only(
        self, album_uri: str, folder_path: Path, extensions: set[str] | None = None
    ) -> tuple[list[dict[str, str]], int]:
        """Upload files not already in the album (MD5 dedup). See ``upload(dedup=True)``."""
        return self.upload(
            album_uri, folder_path, dedup=True, max_workers=1, extensions=extensions
        )

    def _upload_parallel(
        self, album_uri: str, files: list[Path], max_workers: int
    ) -> list[dict[str, str]]:
        # Assumption: sharing one OAuth1Session across worker threads is safe
        # in practice (urllib3 connection pools are thread-safe). If uploads
        # ever misbehave under load, give each worker its own session.
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
        return uploaded

    def upload_folder(
        self,
        album_uri: str,
        folder_path: Path,
        max_workers: int = 4,
        extensions: set[str] | None = None,
    ) -> list[dict[str, str]]:
        """Parallel upload of every file in a folder. See ``upload(max_workers=N)``."""
        return self.upload(
            album_uri,
            folder_path,
            dedup=False,
            max_workers=max_workers,
            extensions=extensions,
        )[0]

    # --- Bulk operations ---

    def delete_empty_albums(self, parent_node_uri: str) -> int:
        deleted = 0
        for child in self.get_node_children(parent_node_uri):
            if child.get("Type") != "Album":
                continue
            album_uri = _album_uri_from_node(child, child.get("Uri", "?"))
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
