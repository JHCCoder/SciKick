"""scikick — Local server entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import HOST, PORT, LOCAL_CACHE_DIR

# Ensure cache directory exists
LOCAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("paper-assistant")

# How often the background loop digests pending chat exchanges and syncs
# memory to Drive (only when dirty). Off the chat path — never adds reply
# latency.
_MEMORY_SYNC_INTERVAL = 120  # seconds

# Holds the background sync task so we can cancel it on shutdown.
_memory_sync_task: asyncio.Task | None = None


async def _memory_sync_loop() -> None:
    """Periodically digest + sync memory to Drive when dirty.

    Each iteration is wrapped so a transient error never kills the loop.
    """
    from memory_manager import flush_memory, is_memory_dirty

    while True:
        try:
            await asyncio.sleep(_MEMORY_SYNC_INTERVAL)
            if is_memory_dirty():
                logger.debug("Memory dirty — running periodic flush")
                await flush_memory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Memory sync loop error (non-fatal): %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks."""
    global _memory_sync_task
    logger.info("Starting scikick server...")
    # Pre-load any persisted state from local cache (Drive memory sync
    # happens when a project folder is connected).
    _memory_sync_task = asyncio.create_task(_memory_sync_loop())
    try:
        yield
    finally:
        # Best-effort final flush before shutdown so the last exchanges
        # aren't left in the local cache only.
        if _memory_sync_task is not None:
            _memory_sync_task.cancel()
            try:
                await _memory_sync_task
            except asyncio.CancelledError:
                pass
        try:
            from memory_manager import flush_memory

            await flush_memory()
        except Exception as exc:
            logger.warning("Shutdown memory flush failed (non-fatal): %s", exc)
        logger.info("Shutting down scikick server.")


app = FastAPI(
    title="scikick",
    description="AI research companion for brainstorming, writing, and analysis with Google Drive sync",
    version="0.1.0",
    lifespan=lifespan,
)

# Allow the Chrome extension to connect from any origin during development.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^chrome-extension://.*$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    """Health check endpoint — verifies the server is running."""
    return {"status": "ok", "version": "0.1.0"}


@app.post("/server/restart")
async def restart_server():
    """Gracefully restart the server (launchd restarts it if running as a service)."""
    import signal, os, threading

    def _shutdown():
        os.kill(os.getpid(), signal.SIGTERM)

    # Delay slightly so the response is sent before shutdown
    threading.Timer(0.3, _shutdown).start()
    return {"status": "restarting"}


# ---------------------------------------------------------------------------
# Import and mount routers (created as we build each module)
# ---------------------------------------------------------------------------
from drive_sync import router as drive_router
from chat_handler import router as chat_router
from memory_manager import router as memory_router

app.include_router(drive_router, prefix="/drive", tags=["drive"])
app.include_router(chat_router, prefix="/chat", tags=["chat"])
app.include_router(memory_router, prefix="/memory", tags=["memory"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=True, log_level="info")
