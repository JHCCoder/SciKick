"""Configuration for the scikick server."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (parent of this server/ directory)
_dotenv_path = Path(__file__).parent.parent / ".env"
if _dotenv_path.exists():
    load_dotenv(_dotenv_path)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
HOST = os.getenv("REVISION_HOST", "127.0.0.1")
PORT = int(os.getenv("REVISION_PORT", "8742"))

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
LOCAL_CACHE_DIR = Path.home() / ".scikick" / "cache"

# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
GOOGLE_CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS",
    str(Path.home() / ".scikick" / "google_credentials.json"),
)
GOOGLE_TOKEN_FILE = os.getenv(
    "GOOGLE_TOKEN",
    str(Path.home() / ".scikick" / "google_token.json"),
)
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",  # list/download your existing files
    "https://www.googleapis.com/auth/drive.file",       # create/update .scikick_memory.json
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# ---------------------------------------------------------------------------
# LLM Provider — unified multi-provider configuration
# ---------------------------------------------------------------------------

# Provider: "anthropic" | "deepseek" | "glm" | "openai" | "gemini" | "kimi" | "custom"
#   anthropic  → uses Anthropic SDK, model defaults to claude-sonnet-4-6
#   deepseek   → uses OpenAI-compatible SDK, base_url = https://api.deepseek.com
#   glm        → uses OpenAI-compatible SDK, base_url = https://open.bigmodel.cn/api/paas/v4
#   openai     → uses OpenAI SDK, base_url = https://api.openai.com/v1
#   gemini     → uses OpenAI-compatible SDK, base_url = https://generativelanguage.googleapis.com/v1beta/openai
#   kimi       → uses OpenAI-compatible SDK, base_url = https://api.moonshot.cn/v1
#   custom     → uses OpenAI-compatible SDK, base_url = LLM_BASE_URL (required)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()

# API key — use the unified key, or fall back to provider-specific ones
LLM_API_KEY = os.getenv(
    "LLM_API_KEY",
    os.getenv("ANTHROPIC_API_KEY", os.getenv("DEEPSEEK_API_KEY", os.getenv("GLM_API_KEY", os.getenv("OPENAI_API_KEY", os.getenv("GEMINI_API_KEY", os.getenv("MOONSHOT_API_KEY", "")))))),
)

# Model name — if not set, auto-selected based on provider
LLM_MODEL = os.getenv("LLM_MODEL", "")

# Base URL — only used for OpenAI-compatible providers (deepseek, openai, custom)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")

# Provider defaults
PROVIDER_DEFAULTS = {
    "anthropic": {
        "model": "claude-sonnet-4-6",
        "base_url": None,  # Anthropic SDK handles this
    },
    "deepseek": {
        "model": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com",
    },
    "glm": {
        "model": "glm-4-plus",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    "openai": {
        "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
    },
    "gemini": {
        "model": "gemini-2.0-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    "kimi": {
        "model": "moonshot-v1-128k",
        "base_url": "https://api.moonshot.cn/v1",
    },
    "custom": {
        "model": "gpt-4o",  # user should override via LLM_MODEL
        "base_url": LLM_BASE_URL,  # required
    },
}


# Runtime overrides — allow changing LLM config without restarting the server
_runtime_overrides: dict = {}


def set_llm_config(provider: str = None, model: str = None,
                   api_key: str = None, base_url: str = None) -> None:
    """Override LLM config at runtime (takes effect immediately)."""
    global _runtime_overrides
    _runtime_overrides = {}
    if provider:
        _runtime_overrides["provider"] = provider
    if model:
        _runtime_overrides["model"] = model
    if api_key:
        _runtime_overrides["api_key"] = api_key
    if base_url is not None:  # allow empty to clear
        _runtime_overrides["base_url"] = base_url


def _save_runtime_config_to_env() -> None:
    """Persist the current runtime config to the .env file."""
    env_path = _dotenv_path
    config = get_llm_config()

    lines = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()

    def _set_or_append(key: str, value: str):
        for i, line in enumerate(lines):
            if line.startswith(f"{key}=") or line.startswith(f"#{key}="):
                lines[i] = f"{key}={value}"
                return
        lines.append(f"{key}={value}")

    _set_or_append("LLM_PROVIDER", config["provider"])
    _set_or_append("LLM_API_KEY", config["api_key"])
    _set_or_append("LLM_MODEL", config["model"])
    # Only persist base_url for "custom" provider — known providers
    # (anthropic, deepseek, glm, openai) have correct defaults in PROVIDER_DEFAULTS.
    # Persisting them would leak a stale URL when switching providers later.
    if config.get("base_url") and config["provider"] == "custom":
        _set_or_append("LLM_BASE_URL", config["base_url"])

    env_path.write_text("\n".join(lines) + "\n")


def get_llm_config() -> dict:
    """Return the resolved LLM configuration (runtime overrides take precedence)."""
    provider = _runtime_overrides.get("provider") or LLM_PROVIDER
    defaults = PROVIDER_DEFAULTS.get(provider, PROVIDER_DEFAULTS["anthropic"])

    model = _runtime_overrides.get("model") or LLM_MODEL or defaults["model"]

    # Base URL resolution order:
    #   1. Explicit runtime override (set by /chat/configure with a value)
    #   2. Provider default (for known providers like glm, deepseek, openai)
    #   3. LLM_BASE_URL from .env (for "custom" provider, or to override a known one)
    if "base_url" in _runtime_overrides:
        base_url = _runtime_overrides["base_url"]
    elif defaults.get("base_url"):
        base_url = defaults["base_url"]  # provider's known URL takes priority
    else:
        base_url = LLM_BASE_URL  # only for "custom" or unlisted providers

    api_key = _runtime_overrides.get("api_key") or LLM_API_KEY

    # Validate
    if not api_key:
        raise RuntimeError(
            f"No API key configured for provider '{provider}'. "
            f"Open ⚙ Settings in the SciKick side panel to enter your API key."
        )

    if provider == "custom" and not base_url:
        raise RuntimeError(
            "LLM_PROVIDER=custom requires LLM_BASE_URL to be set."
        )

    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
    }


# ---------------------------------------------------------------------------
# Legacy constants (kept for backward compatibility)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = LLM_API_KEY  # used by chat_handler.py
ANTHROPIC_MODEL = LLM_MODEL or PROVIDER_DEFAULTS["anthropic"]["model"]

# ---------------------------------------------------------------------------
# Memory file name inside the Drive folder
# ---------------------------------------------------------------------------
MEMORY_FILE_NAME = ".scikick_memory.json"

# ---------------------------------------------------------------------------
# File processing limits
# ---------------------------------------------------------------------------
MAX_PDF_PAGES = 500
MAX_DOCX_SIZE_MB = 50
MAX_IMAGE_SIZE_MB = 25
CHAT_HISTORY_LIMIT = 50  # number of turns to keep in memory

# ---------------------------------------------------------------------------
# Section headers for scientific paper detection
# ---------------------------------------------------------------------------
SECTION_PATTERNS = [
    r"^(?:#+\s*)?(?:Introduction|Background)",
    r"^(?:#+\s*)?(?:Methods|Materials?\s*(?:and|&)\s*Methods?|Experimental Procedures)",
    r"^(?:#+\s*)?(?:Results|Findings)",
    r"^(?:#+\s*)?(?:Discussion|Conclusions?|Summary)",
    r"^(?:#+\s*)?(?:Supplementary|Supplemental|Supporting Information)",
    r"^(?:#+\s*)?(?:Abstract|Summary)",
    r"^(?:#+\s*)?(?:Acknowledgments?|Funding|Author Contributions)",
    r"^(?:#+\s*)?(?:References|Bibliography|Works Cited)",
]
