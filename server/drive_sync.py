"""Google Drive integration — OAuth2, file listing, download, and memory sync."""

import asyncio
import io
import logging
import pickle
import threading
import time
from functools import partial
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from config import (
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_TOKEN_FILE,
    GOOGLE_SCOPES,
    LOCAL_CACHE_DIR,
    PDF_DEFAULT_MODE,
    PORT,
)

logger = logging.getLogger("paper-assistant.drive")
router = APIRouter()

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

# Per-thread Drive/Sheets services. googleapiclient's default transport is
# httplib2.Http, which is NOT thread-safe. load_context fans concurrent
# downloads across thread-pool workers (asyncio.Semaphore(4) + gather); sharing
# one Http/SSL connection across those workers corrupts OpenSSL 3's
# per-connection GCM state and segfaults — EXC_BAD_ACCESS in
# ossl_gcm_set_ctx_params during SSL_read. A thread-local service gives each
# worker its own Http, so concurrent downloads stay isolated.
_drive_tls = threading.local()
_sheets_tls = threading.local()

# Bumped on OAuth re-auth so every thread drops its cached service and rebuilds
# with the new token. Read/written under _creds_lock.
_service_generation = 0
_creds_lock = threading.Lock()
# (generation, credentials) — credentials are read-only after load (the access
# token is fixed for the process; a refresh happens at most once per generation
# inside _load_credentials), so the single cached object is shared safely
# across all worker threads.
_cached_creds: tuple[int, Optional[Credentials]] = (-1, None)


def _get_credentials(generation: int) -> Optional[Credentials]:
    """Return cached credentials for ``generation``, loading once if needed."""
    global _cached_creds
    cached_gen, cached_creds = _cached_creds
    if cached_gen == generation:
        return cached_creds
    with _creds_lock:
        cached_gen, cached_creds = _cached_creds
        if cached_gen == generation:  # double-checked after acquiring the lock
            return cached_creds
        creds = _load_credentials()
        _cached_creds = (generation, creds)
        return creds


def _invalidate_services() -> None:
    """Drop cached services/credentials so they rebuild on next use.

    Called after OAuth re-auth, replacing the old ``_drive_service = None``
    reset. Bumping the generation invalidates every thread's thread-local
    service at once (thread-locals can't be enumerated from outside their
    owning thread, so a generation stamp is the clean way to evict them).
    """
    global _service_generation
    with _creds_lock:
        _service_generation += 1


def get_drive_service() -> Optional[object]:
    """Return an authorised Drive service, or None if not yet authenticated.

    Each thread gets its own service (and its own httplib2.Http); see
    _drive_tls. The service is rebuilt when the generation advances (re-auth).
    """
    gen = getattr(_drive_tls, "generation", -1)
    if gen == _service_generation:
        return getattr(_drive_tls, "service", None)

    creds = _get_credentials(_service_generation)
    if creds is None:
        _drive_tls.service = None
        _drive_tls.generation = _service_generation
        return None

    svc = build("drive", "v3", credentials=creds)
    _drive_tls.service = svc
    _drive_tls.generation = _service_generation
    return svc


def get_sheets_service() -> Optional[object]:
    """Return an authorised Sheets service, or None if not yet authenticated.

    Per-thread, like get_drive_service — _export_google_sheet runs inside
    download_file's thread-pool worker and would otherwise share the Drive
    service's Http.
    """
    gen = getattr(_sheets_tls, "generation", -1)
    if gen == _service_generation:
        return getattr(_sheets_tls, "service", None)

    creds = _get_credentials(_service_generation)
    if creds is None:
        _sheets_tls.service = None
        _sheets_tls.generation = _service_generation
        return None

    svc = build("sheets", "v4", credentials=creds)
    _sheets_tls.service = svc
    _sheets_tls.generation = _service_generation
    return svc


def _load_credentials() -> Optional[Credentials]:
    """Load saved credentials from disk, refreshing if possible.

    Returns None (not authenticated) if there is no token, the token is
    invalid, or a refresh attempt fails. A failed refresh must never raise —
    /drive/auth/status polls this on every health check, and a revoked/expired
    refresh token (``invalid_grant``) is a normal, recoverable state that
    should surface to the user as "re-authorize", not a 500.
    """
    if not Path(GOOGLE_TOKEN_FILE).exists():
        return None

    with open(GOOGLE_TOKEN_FILE, "rb") as token_f:
        creds = pickle.load(token_f)
    if isinstance(creds, Credentials) and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds)
        except RefreshError as exc:
            # invalid_grant (expired/revoked) is non-retryable — the refresh
            # token is dead, so drop it and let the frontend re-trigger OAuth
            # consent. Retryable errors (transient network/transport) leave
            # the file in place for the next poll to retry.
            if not getattr(exc, "retryable", False):
                logger.warning(
                    "Drive token refresh failed (invalid_grant); clearing stale "
                    "token — re-authorize via /drive/auth/url. Detail: %s", exc
                )
                try:
                    Path(GOOGLE_TOKEN_FILE).unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                logger.warning("Drive token refresh failed (retryable): %s", exc)
            return None
        except Exception as exc:  # network/transport hiccups — keep the token
            logger.warning("Drive token refresh raised %s: %s", type(exc).__name__, exc)
            return None
    return creds if isinstance(creds, Credentials) and creds.valid else None


def _save_credentials(creds: Credentials) -> None:
    """Persist credentials to disk."""
    Path(GOOGLE_TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(GOOGLE_TOKEN_FILE, "wb") as token_f:
        pickle.dump(creds, token_f)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/auth/url")
async def get_auth_url():
    """Redirect to the Google OAuth2 authorisation URL."""
    flow = InstalledAppFlow.from_client_secrets_file(
        GOOGLE_CREDENTIALS_FILE, GOOGLE_SCOPES
    )
    flow.redirect_uri = "http://localhost:8742/drive/auth/callback"
    auth_url, state = flow.authorization_url(
        access_type="offline", prompt="consent"
    )
    _prune_pending_flows()
    _pending_flows[state] = (flow, time.time())
    return RedirectResponse(url=auth_url)


# OAuth flows awaiting their callback, stored as {state: (flow, created_at)}.
# Abandoned flows (user closes the tab mid-auth) would otherwise leak
# InstalledAppFlow objects — which hold client secrets — forever, so entries
# are pruned by TTL on each new auth request.
_PENDING_FLOW_TTL = 600  # seconds (Google auth codes expire in ~10 min anyway)
_pending_flows: dict = {}


def _prune_pending_flows() -> None:
    """Drop pending OAuth flows older than the TTL."""
    now = time.time()
    expired = [s for s, (_, ts) in _pending_flows.items() if now - ts > _PENDING_FLOW_TTL]
    for s in expired:
        _pending_flows.pop(s, None)
        logger.info("Pruned expired OAuth flow (state=%s)", s[:8])


@router.get("/auth/callback")
async def auth_callback(state: str = Query(...), code: str = Query(...)):
    """Handle the OAuth2 callback from Google."""
    entry = _pending_flows.pop(state, None)
    if entry is None:
        raise HTTPException(status_code=400, detail="Unknown or expired state token")
    flow = entry[0]

    flow.redirect_uri = "http://localhost:8742/drive/auth/callback"
    try:
        # fetch_token() does blocking I/O — run it in the thread pool so the
        # event loop stays responsive (exceptions raised in the thread, including
        # the scope-change Warning below, propagate to the caller when awaited).
        await _run_in_thread(flow.fetch_token, code=code)
    except Warning:
        # Scope change warning (e.g., upgrading from read-only to read-write).
        # The token response is valid — fetch_token sets credentials before the
        # validation warning fires, but the raise prevents it. Fetch again without
        # scope validation by constructing credentials directly from the response.
        await _run_in_thread(_exchange_token_for_changed_scope, flow, code)
        logger.warning("OAuth scope changed — token obtained with new scopes")
    _save_credentials(flow.credentials)

    # Re-initialise services (bumps the generation so every thread's cached
    # service rebuilds with the new token on next use).
    _invalidate_services()

    return HTMLResponse(content=_AUTH_SUCCESS_HTML)


# Friendly success page shown in the browser tab after the OAuth consent flow
# completes. The side panel detects the new token on its next /drive/auth/status
# poll (or when the user clicks "Check again").
_AUTH_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SciKick — Google Drive connected</title>
<style>
  body { margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; font-family: -apple-system, BlinkMacSystemFont,
         "Segoe UI", Roboto, sans-serif; background: #f7f8fa; color: #1f2937; }
  .card { background: #fff; border-radius: 14px; padding: 40px 36px; max-width: 420px;
          text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.08); }
  .check { font-size: 52px; line-height: 1; margin-bottom: 12px; }
  h1 { font-size: 20px; margin: 0 0 10px; }
  p { font-size: 15px; line-height: 1.5; color: #4b5563; margin: 0 0 8px; }
  .hint { font-size: 13px; color: #6b7280; margin-top: 16px; }
</style>
</head>
<body>
  <div class="card">
    <div class="check">✅</div>
    <h1>Google Drive connected</h1>
    <p>SciKick is now authenticated. You can close this tab and return to the
       SciKick side panel to access Google Drive and work on your project.</p>
    <p class="hint">In the side panel, click <strong>Check again</strong> if the
       “Use this folder” button hasn’t appeared yet.</p>
  </div>
  <script>
    // This tab was opened by the extension; try to close it automatically.
    setTimeout(() => { try { window.close(); } catch (e) {} }, 1800);
  </script>
</body>
</html>"""


def _exchange_token_for_changed_scope(flow, code: str) -> None:
    """Blocking recovery for an OAuth scope change.

    When ``fetch_token`` raises because the requested scopes differ from the
    previously granted ones, exchange the code directly and build credentials
    without scope validation. Run via :func:`_run_in_thread`.
    """
    import requests
    from google.oauth2.credentials import Credentials as GCreds

    token_response = requests.post(
        flow.client_config["token_uri"],
        data={
            "code": code,
            "client_id": flow.client_config["client_id"],
            "client_secret": flow.client_config["client_secret"],
            "redirect_uri": flow.redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    ).json()
    flow.credentials = GCreds(
        token=token_response.get("access_token"),
        refresh_token=token_response.get("refresh_token"),
        token_uri=flow.client_config["token_uri"],
        client_id=flow.client_config["client_id"],
        client_secret=flow.client_config["client_secret"],
        scopes=GOOGLE_SCOPES,
    )


@router.get("/auth/status")
async def auth_status():
    """Check whether we have valid Google credentials.

    ``credentials_present`` reports whether the OAuth client-secrets file
    exists (the Google Cloud setup step); ``authenticated`` reports whether a
    usable token is on disk. The extension uses the difference to show the
    right onboarding prompt: missing creds → run ``./start.sh --drive``;
    missing token → trigger the in-browser OAuth consent via /drive/auth/url.
    """
    creds = _load_credentials()
    return {
        "authenticated": creds is not None,
        "credentials_present": Path(GOOGLE_CREDENTIALS_FILE).exists(),
    }


# ---------------------------------------------------------------------------
# File listing & download
# ---------------------------------------------------------------------------


def _list_files_flat(folder_id: str) -> list:
    """Return a flat list of file dicts from a Drive folder (recursive)."""
    service = get_drive_service()
    if service is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Google")

    files = []
    page_token = None

    while True:
        response = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
                pageToken=page_token,
                pageSize=1000,
            )
            .execute()
        )

        for f in response.get("files", []):
            files.append(
                {
                    "id": f["id"],
                    "name": f["name"],
                    "mimeType": f.get("mimeType", "unknown"),
                    "size": int(f.get("size", 0)),
                    "modifiedTime": f.get("modifiedTime", ""),
                }
            )

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    # Check sub-folders recursively
    for f in files:
        if f["mimeType"] == "application/vnd.google-apps.folder":
            sub = _list_files_flat(f["id"])
            for s in sub:
                s["name"] = f"{f['name']}/{s['name']}"
            files.extend(sub)

    # Filter out folders from the final list
    files = [f for f in files if f["mimeType"] != "application/vnd.google-apps.folder"]

    return files


@router.get("/folder/{folder_id}/files")
async def list_files(folder_id: str):
    """List all files in a Drive folder (recursive)."""
    files = await _run_in_thread(_list_files_flat, folder_id)
    return {"files": files, "count": len(files)}


@router.get("/file/{file_id}/download")
async def download_file(file_id: str):
    """Download a file from Drive and return its content.

    Blocking Google API calls run in a thread pool so the event loop stays
    responsive for health checks and other requests while downloads are in
    progress (a large PDF or slow Drive response would otherwise stall it).
    """
    return await _run_in_thread(_download_file_sync, file_id)


# MediaIoBaseDownload defaults to a 100 MB chunk, so a large file is fetched
# in a single next_chunk() — one timeout fails the whole download and a retry
# restarts from zero. A 2 MB chunk lets resumed retries accumulate progress.
_DOWNLOAD_CHUNK_SIZE = 2 * 1024 * 1024


def _download_media_with_retry(downloader, file_id: str) -> None:
    """Run a MediaIoBaseDownload to completion, resuming past transient failures.

    Drive reads of large files (e.g. an 11 MB supplement) can time out
    mid-stream ("The read operation timed out"). MediaIoBaseDownload tracks
    progress and resumes from the last byte on the next ``next_chunk()``, so
    retrying the same downloader accumulates progress instead of restarting —
    a marginal timeout that kills a one-shot download completes over a few
    resumed chunks.
    """
    done = False
    attempts = 0
    while not done:
        try:
            _, done = downloader.next_chunk()
        except Exception as exc:
            attempts += 1
            if attempts >= 5:
                logger.error(
                    "Drive download of %s failed after %d attempts: %s",
                    file_id, attempts, exc,
                )
                raise
            logger.warning(
                "Drive download of %s: chunk failed (attempt %d): %s — resuming",
                file_id, attempts, exc,
            )
            time.sleep(1.0 * attempts)


def _download_file_sync(file_id: str) -> dict:
    """Blocking core of :func:`download_file`. Run via :func:`_run_in_thread`."""
    service = get_drive_service()
    if service is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Google")

    # Get file metadata
    file_meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
    mime_type = file_meta.get("mimeType", "")
    file_name = file_meta.get("name", "")

    # Handle Google Docs / Sheets — export as PDF or CSV
    if mime_type == "application/vnd.google-apps.document":
        return _export_google_doc(service, file_id, file_name)
    elif mime_type == "application/vnd.google-apps.spreadsheet":
        return _export_google_sheet(file_id)
    elif mime_type == "application/vnd.google-apps.presentation":
        content = _export_google_file(service, file_id, "application/pdf")
        return {"name": file_name, "mimeType": "application/pdf", "content": content}
    else:
        # Binary download
        request = service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request, chunksize=_DOWNLOAD_CHUNK_SIZE)
        _download_media_with_retry(downloader, file_id)

        return {
            "name": file_name,
            "mimeType": mime_type,
            "content_bytes": buffer.getvalue().hex(),  # hex-encoded for JSON transport
        }


def _export_google_file(service, file_id: str, mime_type: str) -> str:
    """Export a Google-native file to the given MIME type, return base64."""
    import base64

    request = service.files().export_media(fileId=file_id, mimeType=mime_type)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=_DOWNLOAD_CHUNK_SIZE)
    _download_media_with_retry(downloader, file_id)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _export_google_doc(service, file_id: str, file_name: str) -> dict:
    """Export a Google Doc as a PDF so it goes through the full parse_pdf
    pipeline (native text layer + page-level OCR on scanned pages + per-image
    figure OCR). The markdown export drops embedded images, so a Doc with
    figure PNGs would lose them; the PDF export embeds them as extractable
    raster images. Returns the same shape as a binary PDF download.
    """
    request = service.files().export_media(
        fileId=file_id, mimeType="application/pdf"
    )
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=_DOWNLOAD_CHUNK_SIZE)
    _download_media_with_retry(downloader, file_id)
    return {
        "name": file_name,
        "mimeType": "application/pdf",
        "content_bytes": buffer.getvalue().hex(),  # hex-encoded for JSON transport
    }


def _export_google_sheet(file_id: str) -> dict:
    """Read a Google Sheet as structured data."""
    sheets = get_sheets_service()
    if sheets is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Google")

    # Get sheet metadata
    sheet_meta = sheets.spreadsheets().get(spreadsheetId=file_id).execute()
    title = sheet_meta.get("properties", {}).get("title", "Sheet")

    all_sheets = {}
    for sheet in sheet_meta.get("sheets", []):
        sheet_name = sheet["properties"]["title"]
        result = (
            sheets.spreadsheets()
            .values()
            .get(spreadsheetId=file_id, range=sheet_name)
            .execute()
        )
        all_sheets[sheet_name] = result.get("values", [])

    return {
        "name": title,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "sheets": all_sheets,
    }


# ---------------------------------------------------------------------------
# Memory file sync
# ---------------------------------------------------------------------------


@router.get("/folder/{folder_id}/memory")
async def load_memory_file(folder_id: str):
    """Load .scikick_memory.json from the Drive folder.

    Blocking Google API calls run in a thread pool so the event loop stays
    responsive while the memory file is searched and downloaded.
    """
    return await _run_in_thread(_load_memory_file_sync, folder_id)


def _load_memory_file_sync(folder_id: str) -> dict:
    """Blocking core of :func:`load_memory_file`. Run via :func:`_run_in_thread`."""
    service = get_drive_service()
    if service is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Google")

    from config import MEMORY_FILE_NAME

    # Search for the memory file
    response = (
        service.files()
        .list(
            q=f"'{folder_id}' in parents and name = '{MEMORY_FILE_NAME}' and trashed = false",
            fields="files(id, name, modifiedTime)",
        )
        .execute()
    )

    files = response.get("files", [])
    if not files:
        return {"exists": False, "memory": None}

    memory_id = files[0]["id"]
    request = service.files().get_media(fileId=memory_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=_DOWNLOAD_CHUNK_SIZE)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    import json

    memory = json.loads(buffer.getvalue().decode("utf-8"))
    return {"exists": True, "memory": memory, "modifiedTime": files[0]["modifiedTime"]}


async def _run_in_thread(fn, *args, **kwargs):
    """Run a blocking call in the default thread pool to avoid blocking the event loop.

    Use ``get_running_loop()`` (not the deprecated ``get_event_loop()``) — this
    is only ever awaited from inside a running event loop, so a running loop
    always exists.
    """
    return await asyncio.get_running_loop().run_in_executor(
        None, partial(fn, *args, **kwargs)
    )


async def _save_memory_to_drive(folder_id: str, memory: dict) -> None:
    """Save a memory dict as .scikick_memory.json in the Drive folder.

    Runs blocking Google API calls in a thread pool so the event loop stays
    responsive for health checks and other requests while the Drive upload
    is in progress.
    """
    service = get_drive_service()
    if service is None:
        raise RuntimeError("Not authenticated with Google")

    import json
    from config import MEMORY_FILE_NAME
    from googleapiclient.http import MediaIoBaseUpload

    content = json.dumps(memory, indent=2, default=str)
    media = io.BytesIO(content.encode("utf-8"))

    # Check if the memory file already exists (run in thread pool)
    response = await _run_in_thread(
        service.files()
        .list(
            q=f"'{folder_id}' in parents and name = '{MEMORY_FILE_NAME}' and trashed = false",
            fields="files(id)",
        )
        .execute,
    )

    existing = response.get("files", [])
    # Simple (non-resumable) upload — the memory file is a tiny JSON blob
    # (a few KB). Resumable uploads add pointless round-trips/overhead
    # designed for large media; pointless here.
    upload = MediaIoBaseUpload(media, mimetype="application/json", resumable=False)

    if existing:
        await _run_in_thread(
            service.files().update(fileId=existing[0]["id"], media_body=upload).execute,
        )
        logger.info("Updated .scikick_memory.json in Drive folder %s", folder_id)
    else:
        file_metadata = {
            "name": MEMORY_FILE_NAME,
            "parents": [folder_id],
            "mimeType": "application/json",
        }
        await _run_in_thread(
            service.files().create(body=file_metadata, media_body=upload).execute,
        )
        logger.info("Created .scikick_memory.json in Drive folder %s", folder_id)


@router.post("/folder/{folder_id}/memory")
async def save_memory_file(folder_id: str, memory: dict):
    """Save .scikick_memory.json to the Drive folder."""
    try:
        await _save_memory_to_drive(folder_id, memory)
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return {"status": "saved"}


# ---------------------------------------------------------------------------
# Load context — download + process files from Drive into chat context
# ---------------------------------------------------------------------------


@router.post("/folder/{folder_id}/load-context")
async def load_context(folder_id: str, force: bool = False):
    """
    Download the manuscript and reviewer comments from Drive,
    parse them, and load into the chat handler's active context.

    Uses file modification times to skip re-processing unchanged files.
    Pass ?force=true to re-download everything regardless.
    """
    service = get_drive_service()
    if service is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Google")

    from file_processor import (
        PaperDocument,
        ReviewerComment,
        extract_reviewer_comments,
        extract_reviewer_comments_from_sheets,
    )
    from chat_handler import (
        set_project_context,
        set_project_file_index,
        set_project_docs,
        set_project_summary,
        get_project_file_counts,
        _current_doc,
        _current_comments,
        _current_doc_source,
        _focused_file_cache,
        _loaded_docs,
        _project_docs,
        _project_summary,
        _project_structure,
    )

    # 1. Initialise or retrieve memory
    from memory_manager import (
        get_current_memory,
        update_paper_sections,
        ReviewerCommentState,
        create_fresh_memory,
        set_current_memory,
        _save_local,
        goal_payload,
    )

    memory = get_current_memory()
    if memory is None:
        folder_meta = await _run_in_thread(
            service.files().get(fileId=folder_id, fields="name").execute
        )
        memory = create_fresh_memory(
            folder_id=folder_id,
            folder_name=folder_meta.get("name", ""),
        )

    previous_snapshots = memory.file_snapshots if not force else {}

    # 2. List all files with modification times
    all_files = await _run_in_thread(_list_files_flat, folder_id)
    logger.info("load-context: %d files in folder", len(all_files))

    # Build a map of current file states
    current_snapshots: dict[str, str] = {}
    for f in all_files:
        current_snapshots[f["id"]] = f.get("modifiedTime", "")

    # 3. Check what changed (skip the memory file — we write it ourselves)
    from config import MEMORY_FILE_NAME
    memory_file = None
    for f in all_files:
        if f["name"] == MEMORY_FILE_NAME:
            memory_file = f["id"]
            break

    changed_ids = set()
    new_ids = set()
    for fid, mtime in current_snapshots.items():
        if fid == memory_file:
            continue  # we write this file, ignore its changes
        if fid not in previous_snapshots:
            new_ids.add(fid)
        elif previous_snapshots[fid] != mtime:
            changed_ids.add(fid)

    unchanged_ids = set(current_snapshots) - changed_ids - new_ids - {memory_file}

    # Only skip re-parse if the current doc actually came from THIS folder
    # (a scraped webpage would have a different source and must be replaced)
    same_folder = _current_doc_source == f"drive:{folder_id}"
    # Enforce "at most one project loaded": when switching to a DIFFERENT
    # folder, drop kept documents from the previous project so they can't
    # bleed into the new one. (.clear() mutates the shared list — reassigning
    # would only rebind this local name.) Same-folder reloads keep them.
    if not same_folder and _loaded_docs:
        n = len(_loaded_docs)
        _loaded_docs.clear()
        logger.info("load-context: switched folder — cleared %d kept document(s)", n)
    if (
        not force
        and not new_ids
        and not changed_ids
        and same_folder
        and (_current_doc is not None or _project_structure)
    ):
        logger.info("load-context: no files changed — skipping re-parse")
        if _current_doc is not None:
            manuscript_payload = {
                "name": _current_doc.title,
                "title": _current_doc.title,
                "sections": [s.heading for s in _current_doc.sections],
                "figures": [f.filename for f in _current_doc.figures],
                "full_text_length": len(_current_doc.full_text),
            }
        else:
            manuscript_payload = None
        return {
            "status": "unchanged",
            "manuscript": manuscript_payload,
            "comments": {
                "count": len(_current_comments),
                "by_reviewer": _count_by(_current_comments, "reviewer"),
                "by_severity": _count_by(_current_comments, "severity"),
            },
            "images_cached": 0,
            "comment_files_processed": [],
            "files_changed": 0,
            "files_skipped": len(all_files),
            "summary": _project_summary,
            "file_counts": get_project_file_counts(),
        }

    if force:
        logger.info("load-context: force reload — re-parsing all files")
    else:
        logger.info(
            "load-context: %d new, %d changed, %d unchanged (of %d total)",
            len(new_ids), len(changed_ids), len(unchanged_ids), len(all_files),
        )

    # Any file may have changed on Drive since the last load — drop the
    # focused-file cache so a subsequent "scan <file>" re-downloads fresh
    # content instead of returning a stale parse from before the edit.
    if force or new_ids or changed_ids:
        _focused_file_cache.clear()

    # 4. Find the manuscript. A clear manuscript is parsed and promoted to
    # _current_doc as before — but if none is detected we no longer abort the
    # load. The folder is still examined: comments, images, supporting/supplement
    # docs, the file index, and the project summary all run with doc=None, and
    # the response carries manuscript: null. This makes a missing manuscript a
    # normal state (empty folder, or a non-manuscript mode like grant/brainstorm)
    # instead of an error.
    manuscript_file = _find_manuscript(all_files)
    doc: Optional[PaperDocument] = None
    ms_id = manuscript_file["id"] if manuscript_file else None
    name = manuscript_file["name"] if manuscript_file else (memory.project_folder_name or "Project")

    if manuscript_file is not None:
        if not force and ms_id not in new_ids and ms_id not in changed_ids and _current_doc is not None:
            # Manuscript unchanged — reuse existing parsed document
            doc = _current_doc
            logger.info("load-context: manuscript unchanged, reusing cached parse")
        else:
            logger.info("load-context: parsing manuscript '%s' (%s)", name, manuscript_file["mimeType"])
            manuscript_content = await download_file(ms_id)
            doc = _parse_downloaded(manuscript_file, manuscript_content, pdf_mode=PDF_DEFAULT_MODE, figure_ocr=False)
    else:
        logger.info("load-context: no manuscript detected — examining folder contents only")

    # 5. Find and extract reviewer comments (only from changed/new files)
    comments: list[ReviewerComment] = []
    comment_files = _find_comment_files(all_files)
    changed_comment_files = []
    skipped_comment_files = []

    if force or (_current_doc is None):
        # Parse all comment files
        changed_comment_files = comment_files
    else:
        for cf in comment_files:
            if cf["id"] in new_ids or cf["id"] in changed_ids:
                changed_comment_files.append(cf)
            else:
                skipped_comment_files.append(cf)
        # Keep existing comments from unchanged files
        if not force and _current_comments and skipped_comment_files:
            comments = list(_current_comments)
            logger.info("load-context: keeping %d existing comments, parsing %d changed comment files",
                        len(comments), len(changed_comment_files))

    for cf in changed_comment_files:
        try:
            downloaded = await download_file(cf["id"])
            mt = cf["mimeType"]

            if mt == "application/vnd.google-apps.spreadsheet" and "sheets" in downloaded:
                comments.extend(extract_reviewer_comments_from_sheets(downloaded["sheets"]))
            elif mt == "application/vnd.google-apps.document" and "content_bytes" in downloaded:
                # Google Doc comment file — now exported as PDF. Parse it and
                # extract comments from the text. figure OCR is off — comments
                # live in the text layer, not inside figures.
                from file_processor import parse_pdf
                cdoc = parse_pdf(bytes.fromhex(downloaded["content_bytes"]), cf["name"], mode=PDF_DEFAULT_MODE, figure_ocr=False)
                comments.extend(extract_reviewer_comments(cdoc.full_text))
            elif "text" in downloaded:
                comments.extend(extract_reviewer_comments(downloaded["text"]))
            elif "text/markdown" in (mt,):
                comments.extend(extract_reviewer_comments(downloaded.get("text", "")))
            elif "content_bytes" in downloaded:
                raw = bytes.fromhex(downloaded["content_bytes"]).decode("utf-8", errors="replace")
                comments.extend(extract_reviewer_comments(raw))
        except Exception as exc:
            logger.warning("load-context: failed to extract comments from %s: %s", cf["name"], exc)

    logger.info("load-context: %d reviewer comments total", len(comments))

    # 6. Only download new/changed images
    images: dict[str, bytes] = {}
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".tiff", ".tif", ".bmp", ".svg"}
    image_files = [
        f for f in all_files
        if any(f["name"].lower().endswith(ext) for ext in image_extensions)
    ]
    changed_images = [f for f in image_files if f["id"] in new_ids or f["id"] in changed_ids]

    for img_file in (changed_images if not force else image_files)[:50]:
        try:
            downloaded = await download_file(img_file["id"])
            if "content_bytes" in downloaded:
                images[img_file["name"]] = bytes.fromhex(downloaded["content_bytes"])
        except Exception as exc:
            logger.warning("load-context: failed to download image %s: %s", img_file["name"], exc)

    logger.info("load-context: %d images cached (%d new/changed)", len(images), len(changed_images))

    # 7. Load into chat handler
    # Pass the manuscript's Drive file id so a later scan/focus request for
    # that same file can reuse the in-memory parse instead of re-downloading.
    set_project_context(doc, comments, images, source=f"drive:{folder_id}", doc_file_id=ms_id, doc_file_name=name)
    set_project_file_index(all_files, manuscript_file_id=ms_id)  # file name→id index + classified structure summary

    # 7.5 Parse every OTHER text-bearing file (supplements, supporting docs)
    # so its chunks are searchable each turn. The manuscript (ms_id), the
    # reviewer-comment files (already parsed into structured comments above),
    # images, the memory file, and non-text-parseable ('miscellaneous') files
    # are skipped. Downloads run with a bounded concurrency to keep the event
    # loop responsive; each file is guarded so one failure can't abort the load.
    comment_ids = {cf["id"] for cf in comment_files}
    image_ids = {f["id"] for f in image_files}
    handled_ids = {ms_id} | comment_ids | image_ids | {memory_file} - {None}

    # Reuse parses from the previous load for unchanged files (within a running
    # session; on restart _project_docs is empty so everything re-parses).
    cached_by_id = {d["file_id"]: d for d in _project_docs if d.get("file_id")}

    async def _parse_one(f: dict):
        fid = f["id"]
        fname = f["name"]
        ftype = classify_project_file(fname, f.get("mimeType", ""), ms_id, fid)
        # Only 'supporting'/'supplement' text files go through here — the
        # manuscript is _current_doc, comments are structured, miscellaneous
        # (binary) files have no text to extract.
        if ftype not in ("supporting", "supplement"):
            return None
        if int(f.get("size", 0) or 0) > _MAX_PARSE_BYTES:
            logger.info("load-context: skipping %s (size > %d bytes)", fname, _MAX_PARSE_BYTES)
            return None
        # Reuse the cached parse when the file is unchanged on a reload.
        cached = cached_by_id.get(fid)
        if (
            cached is not None
            and not force
            and fid not in new_ids
            and fid not in changed_ids
        ):
            return {"file_id": fid, "name": fname, "type": ftype, "doc": cached["doc"]}
        try:
            downloaded = await download_file(fid)
            parsed = _parse_downloaded(f, downloaded)
            logger.info("load-context: parsed '%s' (%s, %d chars)",
                        fname, ftype, len(parsed.full_text))
            return {"file_id": fid, "name": fname, "type": ftype, "doc": parsed}
        except Exception as exc:
            logger.warning("load-context: failed to parse %s: %s", fname, exc)
            # Fall back to a cached parse if we have one, else skip.
            if cached is not None:
                return {"file_id": fid, "name": fname, "type": ftype, "doc": cached["doc"]}
            return None

    sem = asyncio.Semaphore(4)

    async def _bounded(f: dict):
        async with sem:
            return await _parse_one(f)

    parse_candidates = [f for f in all_files if f["id"] not in handled_ids and f["id"]]
    results = await asyncio.gather(*[_bounded(f) for f in parse_candidates])
    extra_docs = [r for r in results if r is not None]
    set_project_docs(extra_docs)
    logger.info("load-context: parsed %d non-manuscript text file(s)", len(extra_docs))

    # 7.6 Best-effort project summary (≤2 sentences). Normally led by the
    # manuscript title + a small sample of each parsed file; when there's no
    # manuscript, inferred from the parsed supporting/supplement docs (or a
    # neutral default for a truly empty folder). Times out / falls back so it
    # can never block or fail the load.
    fallback_title = (doc.title if doc else None) or name
    if doc is None and not extra_docs:
        # Empty folder (no manuscript, nothing parseable) — skip the LLM call.
        summary_text = f"{name}: no documents found yet — paste text or scan a file to begin."
    else:
        summary_text = fallback_title
        try:
            from memory_digest import generate_project_summary
            samples = [(d["name"], d["doc"].full_text[:1500]) for d in extra_docs]
            # Lead with the manuscript excerpt so the summary reflects the main text.
            if doc is not None:
                samples.insert(0, (name, doc.full_text[:1500]))
            summary_text = await asyncio.wait_for(
                generate_project_summary(fallback_title, samples), timeout=30
            )
            logger.info("load-context: project summary: %s", summary_text[:200])
        except asyncio.TimeoutError:
            logger.warning("load-context: summary timed out — using title fallback")
            summary_text = fallback_title
        except Exception as exc:
            logger.warning("load-context: summary failed (%s) — using title fallback", exc)
            summary_text = fallback_title
    set_project_summary(summary_text)

    # 8. Update memory with paper sections, comment states, and file snapshots
    if doc is not None:
        update_paper_sections([
            {"heading": s.heading, "content": s.content}
            for s in doc.sections
        ])

    # Only update comments if we re-parsed
    if changed_comment_files or force:
        memory.reviewer_comments = []
        for c in comments:
            memory.reviewer_comments.append(
                ReviewerCommentState(
                    id=c.id,
                    source=f"{c.reviewer} (comment #{c.comment_number})",
                    text=c.text,
                    severity=c.severity,
                    related_sections=c.related_sections,
                    related_figures=c.related_figures,
                )
            )

    # Update file snapshots for change detection next time
    memory.file_snapshots = current_snapshots

    _save_local(memory)

    # Sync to Google Drive (runs in thread pool to keep event loop responsive)
    try:
        await _save_memory_to_drive(folder_id, memory.model_dump())
    except Exception as exc:
        logger.warning("load-context: Drive sync failed (non-fatal): %s", exc)

    return {
        "status": "loaded",
        "files_changed": len(changed_ids) + len(new_ids),
        "files_skipped": len(unchanged_ids),
        "goal": goal_payload(),
        "manuscript": (
            {
                "name": name,
                "title": doc.title,
                "sections": [s.heading for s in doc.sections],
                "figures": [f.filename for f in doc.figures],
                "full_text_length": len(doc.full_text),
                "reused": not force and ms_id not in new_ids and ms_id not in changed_ids and _current_doc is not None,
                "pdf_parse_mode": getattr(doc, "parse_mode", "fast"),
                "ocr_pages": list(getattr(doc, "ocr_pages", [])),
                "ocr_deficient_pages": list(getattr(doc, "ocr_deficient_pages", [])),
                "ocr_deficient_reason": getattr(doc, "ocr_deficient_reason", ""),
            }
            if doc is not None
            else None
        ),
        "comments": {
            "count": len(comments),
            "by_reviewer": _count_by(comments, "reviewer"),
            "by_severity": _count_by(comments, "severity"),
        },
        "images_cached": len(images),
        "comment_files_processed": [cf["name"] for cf in changed_comment_files],
        "comment_files_skipped": [cf["name"] for cf in skipped_comment_files],
        "summary": summary_text,
        "file_counts": get_project_file_counts(),
        "parsed_files": {
            "manuscript": 1 if doc is not None else 0,
            "comments": len(comment_files),
            "supplement": sum(1 for d in extra_docs if d["type"] == "supplement"),
            "supporting": sum(1 for d in extra_docs if d["type"] == "supporting"),
            "miscellaneous": sum(
                1 for f in all_files
                if f["id"] not in handled_ids
                and f["id"]
                and not _is_text_parseable(f["name"], f.get("mimeType", ""))
            ),
        },
    }


# ---------------------------------------------------------------------------
# Context-loading helpers
# ---------------------------------------------------------------------------


# --- File-type classification keywords (shared by _find_manuscript,
# _find_comment_files, and classify_project_file) -------------------------
MANUSCRIPT_KEYWORDS = ["manuscript", "paper", "draft", "article", "submission"]
# Substrings that mark a file as supporting material, not the main text.
# Kept conservative to avoid false positives on legitimate filenames.
SUPPLEMENTAL_MARKERS = (
    "supplement", "supplementary", "supplemental",
    "supporting information", "supporting-material",
    "appendix", "response to reviewer", "cover letter",
)
# Substrings that mark a file as the main text.
MAIN_MARKERS = ("main submission", "main text", "main manuscript",
                "main_manuscript", "main_submission", "full text",
                "main document")
COMMENT_KEYWORDS = [
    "reviewer", "review", "comment", "feedback",
    "referee", "response", "decision",
]

# MIME types / extensions we can extract readable text from. Files outside
# this set (images, videos, archives, unknown binaries) are classed
# 'miscellaneous' and never parsed — there's no text to retrieve.
_TEXT_PARSEABLE_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "text/plain",
    "text/markdown",
    "application/vnd.google-apps.document",        # Google Doc → exported as markdown
    "application/vnd.google-apps.spreadsheet",     # Google Sheet → rows
    "application/vnd.google-apps.presentation",    # exported as PDF
}
_TEXT_PARSEABLE_EXTS = (
    ".pdf", ".docx", ".txt", ".md", ".markdown", ".csv", ".tsv",
    ".py", ".r", ".sh", ".tex", ".bib", ".json", ".xml", ".html", ".htm",
)

# Skip files larger than this when parsing non-manuscript docs — protects the
# event loop from pathological binary blobs mis-detected as text. The
# manuscript is exempt (it's always parsed); this only guards the "parse every
# other file" pass.
_MAX_PARSE_BYTES = 50 * 1024 * 1024


def _is_text_parseable(name: str, mime: str) -> bool:
    """True if we have a parser that can extract text from this file."""
    if mime in _TEXT_PARSEABLE_MIMES:
        return True
    return name.lower().endswith(_TEXT_PARSEABLE_EXTS)


def classify_project_file(
    name: str, mime: str, manuscript_file_id: str, file_id: str
) -> str:
    """Classify a project file for the structure summary.

    Returns one of 'manuscript', 'supplement', 'reviewer_comment',
    'supporting', 'miscellaneous'. The manuscript is identified by Drive file
    id (the file _find_manuscript picked); supplements and reviewer-comment
    files by filename keywords. Remaining files split into 'supporting' (we
    can extract text from them — data, code, notes, drafts) or
    'miscellaneous' (binary/unreadable: images, archives, unknown). Used for
    the structure overview shown to the model and to decide which files to
    chunk on Load Project.
    """
    if manuscript_file_id and file_id == manuscript_file_id:
        return "manuscript"
    n = name.lower()
    if any(m in n for m in SUPPLEMENTAL_MARKERS):
        return "supplement"
    # Reviewer-comment files: match comment keywords but exclude files that
    # look like the manuscript itself (mirrors _find_comment_files).
    if any(kw in n for kw in COMMENT_KEYWORDS) and not any(
        kw in n for kw in ("manuscript", "paper", "draft", "article")
    ):
        return "reviewer_comment"
    return "supporting" if _is_text_parseable(name, mime) else "miscellaneous"


def _parse_downloaded(file_dict: dict, downloaded: dict, pdf_mode: str = "auto", figure_ocr: bool = False):
    """Dispatch a downloaded Drive file to the right text parser.

    Shared by the manuscript path and the "parse every other text file" pass
    in load_context. ``downloaded`` is the dict returned by download_file()
    (keys vary by type: ``content_bytes`` hex for binaries, ``text`` for
    Google Docs, ``sheets`` for Google Sheets). Returns a PaperDocument.
    ``pdf_mode`` is forwarded to parse_pdf ("fast" | "auto"); non-PDF files
    ignore it. ``figure_ocr`` (default False) is forwarded to parse_pdf /
    parse_docx — Load Project skips per-image figure OCR for speed; page-level
    OCR on scanned PDF pages still runs. Scan-and-keep re-parses with True.
    """
    from file_processor import parse_pdf, parse_docx, parse_text

    name = file_dict["name"]
    mime = file_dict.get("mimeType", "")
    size = int(file_dict.get("size", 0) or 0)

    if mime == "application/pdf":
        return parse_pdf(bytes.fromhex(downloaded["content_bytes"]), name, mode=pdf_mode, figure_ocr=figure_ocr)
    if mime == "application/vnd.google-apps.document":
        # Google Doc — _export_google_doc exports it as PDF (content_bytes) so
        # embedded figures get OCR'd via parse_pdf. Falls through to parse_text
        # only for legacy callers still returning markdown text.
        if "content_bytes" in downloaded:
            return parse_pdf(bytes.fromhex(downloaded["content_bytes"]), name, mode=pdf_mode, figure_ocr=figure_ocr)
        return parse_text(downloaded.get("text", ""), name)
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return parse_docx(bytes.fromhex(downloaded["content_bytes"]), name, figure_ocr=figure_ocr)
    if mime == "text/markdown" or "text" in downloaded:
        return parse_text(downloaded.get("text", ""), name)
    if "sheets" in downloaded:
        # Flatten a Google Sheet into text so it chunks like any other doc.
        lines = []
        for sheet_name, rows in downloaded["sheets"].items():
            lines.append(f"[{sheet_name}]")
            for row in rows:
                lines.append("\t".join(str(c) for c in row))
        return parse_text("\n".join(lines), name)
    # Binary fallback: try to decode, else pick a parser by extension.
    content_bytes = bytes.fromhex(downloaded.get("content_bytes", ""))
    if name.lower().endswith(".pdf"):
        return parse_pdf(content_bytes, name, mode=pdf_mode, figure_ocr=figure_ocr)
    if name.lower().endswith(".docx"):
        return parse_docx(content_bytes, name, figure_ocr=figure_ocr)
    return parse_text(content_bytes.decode("utf-8", errors="replace"), name)


def _find_manuscript(files: list[dict]) -> Optional[dict]:
    """Find the most likely manuscript file in a list of Drive files.

    A revision folder typically holds the main manuscript *and* a supplemental
    material file — often both .docx, both with "manuscript" in the name, and
    the supplement is frequently the LARGER file (embedded figures). So neither
    "first keyword match" nor "largest preferred-type file" reliably picks the
    main text. We instead (1) strongly prefer files whose name says "main",
    and (2) exclude anything that looks like supporting material.
    """
    preferred_types = [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
        "application/pdf",
        "application/vnd.google-apps.document",
    ]

    def name(f: dict) -> str:
        return f["name"].lower()

    def is_supplement(f: dict) -> bool:
        n = name(f)
        return any(m in n for m in SUPPLEMENTAL_MARKERS)

    def is_main(f: dict) -> bool:
        n = name(f)
        return any(m in n for m in MAIN_MARKERS)

    def largest(seq: list[dict]) -> Optional[dict]:
        # Tie-break by size so a multi-file folder still picks deterministically.
        seq.sort(key=lambda f: f["size"], reverse=True)
        return seq[0] if seq else None

    candidates = [f for f in files if f["mimeType"] in preferred_types]

    # 1. A preferred-type file explicitly named "main" that isn't a supplement.
    main_candidates = [f for f in candidates if is_main(f) and not is_supplement(f)]
    if main_candidates:
        return largest(main_candidates)

    # 2. A preferred-type file matching a manuscript keyword, excluding
    #    supplements (this is where the old code went wrong — it matched the
    #    supplement because "manuscript" appears in its name too).
    keyword_candidates = [
        f for f in candidates
        if not is_supplement(f) and any(kw in name(f) for kw in MANUSCRIPT_KEYWORDS)
    ]
    if keyword_candidates:
        return largest(keyword_candidates)

    # 3. Any non-supplement preferred-type file.
    non_supp = [f for f in candidates if not is_supplement(f)]
    if non_supp:
        return largest(non_supp)

    # 4. Last resort: largest preferred-type file even if it looks supplemental
    #    (better than nothing — the folder may contain only the supplement).
    if candidates:
        return largest(candidates)

    # 5. No preferred type — fall back to a paper-like extension.
    for ext in (".pdf", ".docx"):
        for f in files:
            if name(f).endswith(ext):
                return f

    return None


def _find_comment_files(files: list[dict]) -> list[dict]:
    """Find files likely to contain reviewer comments."""
    comment_types = [
        "text/plain",
        "text/markdown",
        "application/vnd.google-apps.spreadsheet",
        "application/vnd.google-apps.document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]

    results = []
    for f in files:
        name_lower = f["name"].lower()
        # Skip the manuscript itself
        if any(kw in name_lower for kw in ("manuscript", "paper", "draft", "article")):
            continue
        # Match comment keywords, but only on file types we can actually parse
        # as comments. (An earlier second loop here appended keyword-named files
        # of ANY type — .zip, .png, .pdf — which then failed silently in
        # load_context's comment extractor.)
        if any(kw in name_lower for kw in COMMENT_KEYWORDS):
            if f["mimeType"] in comment_types or name_lower.endswith((".txt", ".md")):
                results.append(f)

    return results


def _count_by(items: list, attr: str) -> dict:
    """Count items by an attribute value."""
    counts: dict = {}
    for item in items:
        val = getattr(item, attr, "unknown")
        counts[val] = counts.get(val, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Resume endpoint — loads everything needed for cross-computer resume
# ---------------------------------------------------------------------------


@router.get("/folder/{folder_id}/resume")
async def resume_project(folder_id: str):
    """
    One-shot endpoint for cross-computer resume.
    Lists files, loads memory from Drive, restores all state.
    """
    import json

    service = get_drive_service()
    if service is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Google")

    auth_url_hint = (
        f"http://localhost:{PORT}/drive/auth/url"
    )

    def _folder_access_detail() -> str:
        return (
            f"Couldn't access that Google Drive folder (ID: {folder_id}). "
            "Check that the URL/ID is correct and that the folder is shared with "
            "the Google account you signed in with. To switch accounts, run "
            "`./start.sh --drive` or visit " + auth_url_hint + " in your browser."
        )

    # 1. List files
    try:
        files = await _run_in_thread(_list_files_flat, folder_id)
    except HttpError as exc:
        status = exc.resp.status if exc.resp is not None else None
        if status in (403, 404):
            raise HTTPException(status_code=404, detail=_folder_access_detail())
        raise HTTPException(status_code=502, detail=f"Google Drive error: {exc}")

    # 2. Check for existing memory file
    memory_result = await load_memory_file(folder_id)
    has_memory = memory_result.get("exists", False)
    memory_data = memory_result.get("memory")

    # 3. Get folder metadata
    try:
        folder_meta = await _run_in_thread(
            service.files().get(fileId=folder_id, fields="name").execute
        )
    except HttpError as exc:
        status = exc.resp.status if exc.resp is not None else None
        if status in (403, 404):
            raise HTTPException(status_code=404, detail=_folder_access_detail())
        raise HTTPException(status_code=502, detail=f"Google Drive error: {exc}")
    folder_name = folder_meta.get("name", "Project")

    # 4. If memory exists, restore it into the memory manager
    resume_info = None
    if has_memory and memory_data:
        from memory_manager import (
            RevisionMemory,
            ReviewerCommentState,
            PaperSectionSummary,
            Decision,
            ChatTurn,
            set_current_memory,
            _save_local,
            goal_payload,
        )

        try:
            # Build the memory object
            memory = RevisionMemory(**memory_data)
            set_current_memory(memory)
            _save_local(memory)

            # Build a summary of where we left off
            status_counts = {"pending": 0, "in_progress": 0, "resolved": 0, "deferred": 0}
            for c in memory.reviewer_comments:
                status_counts[c.status] = status_counts.get(c.status, 0) + 1

            resume_info = {
                "project_id": memory.project_id,
                "last_computer": memory.last_computer,
                "last_updated": memory.last_updated,
                "total_comments": len(memory.reviewer_comments),
                "resolved": status_counts["resolved"],
                "in_progress": status_counts["in_progress"],
                "pending": status_counts["pending"],
                "chat_turns": len(memory.chat_history) // 2,
                "decisions_count": len(memory.decisions),
                "conversation_summary": memory.conversation_summary[:500],
                "goal": goal_payload(),
            }

            logger.info(
                "Resumed project '%s' from %s: %d comments, %d chat turns",
                folder_name,
                memory.last_computer,
                len(memory.reviewer_comments),
                len(memory.chat_history) // 2,
            )

        except Exception as exc:
            logger.error("Failed to restore memory: %s", exc)
            has_memory = False
            resume_info = {"error": str(exc)}

    return {
        "folder_id": folder_id,
        "folder_name": folder_name,
        "files": files,
        "file_count": len(files),
        "has_memory": has_memory,
        "memory": memory_data if has_memory else None,
        "resume_info": resume_info,
    }


