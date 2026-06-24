"""PhiDkick — Local server entry point."""

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks."""
    logger.info("Starting PhiDkick server...")
    # Pre-load any persisted state from local cache (Drive memory sync
    # happens when a project folder is connected).
    yield
    logger.info("Shutting down PhiDkick server.")


app = FastAPI(
    title="PhiDkick",
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
