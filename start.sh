#!/usr/bin/env bash
# PhiDkick — One-command launcher
#
# Usage:
#   ./start.sh                    # Start the server
#   ./start.sh --install          # Install dependencies first, then start
#   ./start.sh --setup            # First-time setup wizard
#
# Requirements:
#   - Python 3.10+
#   - Chrome/Chromium browser (for the extension)
#   - Google Cloud project with Drive API enabled (for Google Drive access)
#   - Anthropic API key

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$SCRIPT_DIR/server"
VENV_DIR="$SCRIPT_DIR/.venv"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

banner() {
    echo -e "${GREEN}"
    echo "  ╔═══════════════════════════════════════╗"
    echo "  ║         📄 PhiDkick 📄               ║"
    echo "  ║   AI research companion              ║"
    echo "  ╚═══════════════════════════════════════╝"
    echo -e "${NC}"
}

install_deps() {
    echo -e "${BLUE}Setting up Python virtual environment...${NC}"

    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
    fi

    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip -q
    pip install -r "$SERVER_DIR/requirements.txt" -q

    echo -e "${GREEN}✓ Dependencies installed${NC}"
}

first_time_setup() {
    echo -e "${YELLOW}First-time setup wizard${NC}"
    echo ""

    # --- Choose LLM provider ---
    echo -e "${YELLOW}Which LLM provider will you use?${NC}"
    echo "  1) Anthropic (Claude)  — https://console.anthropic.com/"
    echo "  2) DeepSeek             — https://platform.deepseek.com/"
    echo "  3) OpenAI (GPT-4o)      — https://platform.openai.com/"
    echo "  4) Custom (OpenAI-compatible — Ollama, Groq, Together, etc.)"
    echo ""
    read -r -p "Enter choice [1-4] (default: 1): " provider_choice
    provider_choice="${provider_choice:-1}"

    case "$provider_choice" in
        1)
            LLM_PROVIDER="anthropic"
            DEFAULT_MODEL="claude-sonnet-4-6"
            echo -e "${GREEN}Selected: Anthropic (Claude)${NC}"
            echo "Get your API key at: https://console.anthropic.com/"
            ;;
        2)
            LLM_PROVIDER="deepseek"
            DEFAULT_MODEL="deepseek-chat"
            echo -e "${GREEN}Selected: DeepSeek${NC}"
            echo "Get your API key at: https://platform.deepseek.com/"
            ;;
        3)
            LLM_PROVIDER="openai"
            DEFAULT_MODEL="gpt-4o"
            echo -e "${GREEN}Selected: OpenAI${NC}"
            echo "Get your API key at: https://platform.openai.com/"
            ;;
        4)
            LLM_PROVIDER="custom"
            DEFAULT_MODEL=""
            echo -e "${GREEN}Selected: Custom (OpenAI-compatible)${NC}"
            echo ""
            read -r -p "Enter your provider's base URL (e.g. http://localhost:11434/v1 for Ollama): " custom_url
            export LLM_BASE_URL="$custom_url"
            echo "LLM_BASE_URL=$custom_url" >> "$SCRIPT_DIR/.env" 2>/dev/null || true
            read -r -p "Enter model name (e.g. llama3, mixtral-8x7b): " custom_model
            DEFAULT_MODEL="$custom_model"
            ;;
        *)
            echo -e "${RED}Invalid choice. Defaulting to Anthropic.${NC}"
            LLM_PROVIDER="anthropic"
            DEFAULT_MODEL="claude-sonnet-4-6"
            ;;
    esac

    export LLM_PROVIDER="$LLM_PROVIDER"
    echo "LLM_PROVIDER=$LLM_PROVIDER" > "$SCRIPT_DIR/.env"
    echo ""

    # --- API Key ---
    if [ "$LLM_PROVIDER" = "anthropic" ]; then
        key_var="ANTHROPIC_API_KEY"
        key_url="https://console.anthropic.com/"
    elif [ "$LLM_PROVIDER" = "deepseek" ]; then
        key_var="DEEPSEEK_API_KEY"
        key_url="https://platform.deepseek.com/"
    elif [ "$LLM_PROVIDER" = "openai" ]; then
        key_var="OPENAI_API_KEY"
        key_url="https://platform.openai.com/"
    else
        key_var="LLM_API_KEY"
        key_url="your provider"
    fi

    if [ -z "${!key_var:-}" ] && [ -z "${LLM_API_KEY:-}" ]; then
        echo -e "${YELLOW}API key not found.${NC}"
        echo "Get your key at: $key_url"
        echo ""
        read -r -p "Enter your API key: " api_key
        export LLM_API_KEY="$api_key"
        echo "LLM_API_KEY=$api_key" >> "$SCRIPT_DIR/.env"
        echo ""
        echo -e "${GREEN}✓ API key set for this session${NC}"
        echo "  To make it permanent, add this to your ~/.zshrc:"
        echo "  export LLM_API_KEY='$api_key'"
        echo ""
    else
        echo -e "${GREEN}✓ API key found${NC}"
    fi

    # --- Model ---
    if [ -n "$DEFAULT_MODEL" ]; then
        read -r -p "Model name [default: $DEFAULT_MODEL]: " model_name
        model_name="${model_name:-$DEFAULT_MODEL}"
        export LLM_MODEL="$model_name"
        echo "LLM_MODEL=$model_name" >> "$SCRIPT_DIR/.env"
        echo -e "${GREEN}✓ Using model: $model_name${NC}"
        echo ""
    fi

    # Check Google credentials
    CREDS_DIR="$HOME/.scientific-paper-assistant"
    mkdir -p "$CREDS_DIR"

    if [ ! -f "$CREDS_DIR/google_credentials.json" ]; then
        echo ""
        echo -e "${YELLOW}Google Cloud credentials not found.${NC}"
        echo "To enable Google Drive access, you need a Google Cloud project with the Drive API enabled."
        echo ""
        echo "Steps:"
        echo "  1. Go to https://console.cloud.google.com/"
        echo "  2. Create a project (or select existing)"
        echo "  3. Enable the Google Drive API"
        echo "  4. Create an OAuth 2.0 Client ID (Desktop application type)"
        echo "  5. Download the credentials JSON file"
        echo "  6. Save it to: $CREDS_DIR/google_credentials.json"
        echo ""
    else
        echo -e "${GREEN}✓ Google credentials found${NC}"
    fi

    echo ""
    echo -e "${GREEN}Setup complete!${NC}"
    echo "  Provider: $LLM_PROVIDER"
    echo "  Model: ${LLM_MODEL:-default}"
    echo ""
}

start_server() {
    echo -e "${BLUE}Starting server...${NC}"

    # Check for .env file
    if [ -f "$SCRIPT_DIR/.env" ]; then
        source "$SCRIPT_DIR/.env"
    fi

    # Check for any LLM API key
    if [ -z "${LLM_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
        echo -e "${RED}Error: No LLM API key found.${NC}"
        echo "Run './start.sh --setup' first, or set LLM_API_KEY in your shell."
        exit 1
    fi

    # Check venv
    if [ ! -d "$VENV_DIR" ]; then
        echo -e "${YELLOW}Virtual environment not found. Installing dependencies...${NC}"
        install_deps
    fi

    source "$VENV_DIR/bin/activate"

    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  Server:  http://localhost:8742${NC}"
    echo -e "${GREEN}  Health:  http://localhost:8742/health${NC}"
    echo -e "${GREEN}  API docs: http://localhost:8742/docs${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "${YELLOW}Next steps:${NC}"
    echo "  1. Load the Chrome extension:"
    echo "     → Go to chrome://extensions/"
    echo "     → Enable 'Developer mode'"
    echo "     → Click 'Load unpacked'"
    echo "     → Select: $SCRIPT_DIR/extension"
    echo ""
    echo "  2. Click the extension icon to open the side panel"
    echo ""
    echo "  3. Authenticate with Google (first time only):"
    echo "     → Visit http://localhost:8742/drive/auth/url"
    echo ""
    echo -e "${YELLOW}Press Ctrl+C to stop the server${NC}"
    echo ""

    cd "$SERVER_DIR"
    python3 main.py
}

# --- Main ---
banner

case "${1:-}" in
    --install)
        install_deps
        start_server
        ;;
    --setup)
        install_deps
        first_time_setup
        ;;
    --help|-h)
        echo "Usage: ./start.sh [OPTION]"
        echo ""
        echo "Options:"
        echo "  (none)      Start the server"
        echo "  --install   Install dependencies, then start"
        echo "  --setup     First-time setup wizard"
        echo "  --help      Show this help"
        ;;
    *)
        # Ensure deps are installed if venv exists
        if [ -d "$VENV_DIR" ]; then
            source "$VENV_DIR/bin/activate"
        else
            install_deps
            source "$VENV_DIR/bin/activate"
        fi
        start_server
        ;;
esac
