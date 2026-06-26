#!/usr/bin/env bash
# scikick — One-command launcher
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
    echo "  ║         📄 scikick 📄               ║"
    echo "  ║   AI research companion              ║"
    echo "  ╚═══════════════════════════════════════╝"
    echo -e "${NC}"
}

check_python() {
    # Find a Python 3.10+ interpreter. Sets PYTHON3 variable on success.
    PYTHON3=""

    # Helper: try a candidate, return 0 (and set PYTHON3) if it's 3.10+
    _try_python() {
        local candidate="$1"
        if command -v "$candidate" &>/dev/null; then
            local ver
            ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || return 1
            if [ -n "$ver" ]; then
                local major minor
                major=$(echo "$ver" | cut -d. -f1)
                minor=$(echo "$ver" | cut -d. -f2)
                if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
                    PYTHON3="$candidate"
                    echo -e "${GREEN}✓ Python $ver detected ($candidate)${NC}"
                    return 0
                fi
            fi
        fi
        return 1
    }

    # Scan directories where Python may live, checking versioned + unversioned names
    # Priority: Homebrew > conda > system PATH
    local search_dirs=""
    [ -d /opt/homebrew/bin ] && search_dirs="$search_dirs /opt/homebrew/bin"
    [ -d /usr/local/bin ] && search_dirs="$search_dirs /usr/local/bin"
    [ -d /usr/local/opt/python@3/bin ] && search_dirs="$search_dirs /usr/local/opt/python@3/bin"

    # Also check conda base if present
    local conda_python
    conda_python=$(command -v conda 2>/dev/null)

    # 1) Versioned binaries first: python3.13, python3.12, python3.11, python3.10
    for dir in $search_dirs; do
        for ver in 3.13 3.12 3.11 3.10; do
            _try_python "$dir/python$ver" && return 0
        done
    done
    # Also check conda base for versioned pythons
    if [ -n "$conda_python" ]; then
        local conda_root
        conda_root=$(dirname "$(dirname "$conda_python")")
        for ver in 3.13 3.12 3.11 3.10; do
            _try_python "$conda_root/bin/python$ver" && return 0
        done
    fi

    # 2) Unversioned python3 in known dirs + PATH
    for candidate in \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        /usr/local/opt/python@*/bin/python3 \
        python3; do
        _try_python "$candidate" && return 0
    done

    # 3) Check conda environments for python3
    if [ -n "$conda_python" ]; then
        for env_dir in "$(dirname "$(dirname "$conda_python")")"/envs/*; do
            [ -d "$env_dir/bin" ] && _try_python "$env_dir/bin/python3" && return 0
        done
    fi

    # No suitable Python found — offer to install
    echo -e "${YELLOW}⚠ Python 3.10+ is required but was not found.${NC}"
    echo ""

    if command -v brew &>/dev/null; then
        echo "scikick requires Python 3.10 or newer."
        echo "Your system has an older version, which can cause package installation to fail."
        echo ""
        echo "I can install Python 3.13 via Homebrew. It is isolated — it won't"
        echo "replace your system Python or interfere with other projects."
        echo ""
        read -r -p "Install Python 3.13 via Homebrew? [Y/n]: " resp
        if [ "$resp" = "n" ] || [ "$resp" = "N" ]; then
            echo ""
            echo -e "${RED}Cannot continue without Python 3.10+.${NC}"
            echo "Install it manually, then re-run this script."
            echo "  brew install python@3.13"
            exit 1
        fi

        echo ""
        echo -e "${BLUE}Installing Python 3.13 via Homebrew…${NC}"
        if ! brew install python@3.13; then
            echo -e "${RED}✗ Homebrew install failed.${NC}"
            echo "Try manually: brew install python@3.13"
            exit 1
        fi

        # Find the freshly installed python (versioned name first)
        for candidate in \
            /opt/homebrew/bin/python3.13 \
            /opt/homebrew/bin/python3 \
            /usr/local/bin/python3.13 \
            /usr/local/bin/python3; do
            _try_python "$candidate" && return 0
        done

        echo -e "${RED}✗ Python 3.13 installed but not found on PATH.${NC}"
        echo "Try: export PATH=\"/opt/homebrew/bin:\$PATH\""
        echo "Then re-run this script."
        exit 1
    else
        echo -e "${RED}Cannot continue without Python 3.10+.${NC}"
        echo "Install Python 3.10+ and re-run this script."
        echo "  macOS:  brew install python@3.13"
        echo "  Linux:  sudo apt install python3.12  (or your distro's equivalent)"
        exit 1
    fi
}

install_deps() {
    echo -e "${BLUE}Setting up Python virtual environment...${NC}"

    # Ensure Python 3.10+ is available
    check_python

    if [ ! -d "$VENV_DIR" ]; then
        "$PYTHON3" -m venv "$VENV_DIR"
    fi

    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip
    pip install -r "$SERVER_DIR/requirements.txt"

    echo -e "${GREEN}✓ Dependencies installed${NC}"
}

google_credentials_setup() {
    CREDS_DIR="$HOME/.scikick"
    mkdir -p "$CREDS_DIR"
    CREDS_FILE="$CREDS_DIR/google_credentials.json"

    if [ -f "$CREDS_FILE" ]; then
        # Validate existing credentials
        EXISTING_ID=$("${PYTHON3:-python3}" -c "
import json
try:
    c = json.load(open('$CREDS_FILE'))
    inst = c.get('installed', c)
    cid = inst.get('client_id', '')
    secret = inst.get('client_secret', '')
    if cid and secret:
        print(cid[:30])
    else:
        print('INVALID')
except:
    print('INVALID')
" 2>/dev/null)

        if [ "$EXISTING_ID" != "INVALID" ] && [ -n "$EXISTING_ID" ]; then
            echo -e "${GREEN}✓ Google credentials configured (Client ID: ${EXISTING_ID}…)${NC}"
            echo "  To redo setup, delete $CREDS_FILE and re-run this wizard."
            echo ""
            return 0
        else
            echo -e "${YELLOW}Existing credentials appear invalid. Let's redo the setup.${NC}"
            echo ""
        fi
    fi

    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  Google Drive Setup${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "scikick needs access to your Google Drive to load your papers."
    echo "I'll walk you through this in ~5 minutes."
    echo "You'll need a Google account (any Gmail works)."
    echo ""
    echo -e "${YELLOW}Why this is needed:${NC}"
    echo "  scikick's official Google extension is still pending approval"
    echo "  from Google. Until it's verified, you'll set up your own"
    echo "  personal Google Cloud project so the app can read your Drive."
    echo "  This is free, takes ~5 minutes, and you only do it once."
    echo ""

    # ── Step 1: Create project ──
    echo -e "${YELLOW}Step 1/6: Create a Google Cloud project${NC}"
    echo "A 'project' is just a container for your app settings."
    echo "Name it whatever you like — we recommend 'SciKick'."
    echo ""
    if command -v open &>/dev/null; then
        read -r -p "Open the project creation page in your browser? [Y/n]: " resp
        if [ "$resp" != "n" ] && [ "$resp" != "N" ]; then
            open "https://console.cloud.google.com/projectcreate" 2>/dev/null || true
        fi
    else
        echo "Go to: https://console.cloud.google.com/projectcreate"
    fi
    echo ""
    echo "  → Click the blue 'CREATE' button"
    echo "  → Wait for the notification bell to show 'Project created'"
    read -r -p "Press Enter when your project is ready…"

    # ── Step 2: Enable Drive API ──
    echo ""
    echo -e "${YELLOW}Step 2/6: Enable the Google Drive API${NC}"
    if command -v open &>/dev/null; then
        read -r -p "Open the Drive API page? [Y/n]: " resp
        if [ "$resp" != "n" ] && [ "$resp" != "N" ]; then
            open "https://console.cloud.google.com/apis/library/drive.googleapis.com" 2>/dev/null || true
        fi
    else
        echo "Go to: https://console.cloud.google.com/apis/library/drive.googleapis.com"
    fi
    echo "  → Make sure your project is selected (dropdown at the top)"
    echo "  → Click the blue 'ENABLE' button"
    read -r -p "Press Enter when done…"

    # ── Step 3: Enable Sheets API ──
    echo ""
    echo -e "${YELLOW}Step 3/6: Enable the Google Sheets API${NC}"
    if command -v open &>/dev/null; then
        read -r -p "Open the Sheets API page? [Y/n]: " resp
        if [ "$resp" != "n" ] && [ "$resp" != "N" ]; then
            open "https://console.cloud.google.com/apis/library/sheets.googleapis.com" 2>/dev/null || true
        fi
    else
        echo "Go to: https://console.cloud.google.com/apis/library/sheets.googleapis.com"
    fi
    echo "  → Make sure your project is selected (dropdown at the top)"
    echo "  → Click the blue 'ENABLE' button"
    read -r -p "Press Enter when done…"

    # ── Step 4: OAuth consent screen ──
    echo ""
    echo -e "${YELLOW}Step 4/6: Configure the OAuth consent screen${NC}"
    if command -v open &>/dev/null; then
        read -r -p "Open APIs & Services dashboard? [Y/n]: " resp
        if [ "$resp" != "n" ] && [ "$resp" != "N" ]; then
            open "https://console.cloud.google.com/apis" 2>/dev/null || true
        fi
    else
        echo "Go to: https://console.cloud.google.com/apis"
    fi
    echo ""
    echo "  → Make sure your project is selected (dropdown at the top)"
    echo "  → In the left sidebar, click 'OAuth consent screen'."
    echo "  → If this is a new project, you'll see an Overview page with a"
    echo "    'GET STARTED' button (the OAuth platform isn't configured yet)."
    echo "    Click 'GET STARTED'."
    echo ""
    echo "  In the Overview page that pops up:"
    echo "  App information:"
    echo "  → App name: whatever you like (we recommend 'SciKick')"
    echo "  → User support email: your email address"
    echo "  Audience:"
    echo "  → Select 'External'"
    echo "    (Internal requires a Google Workspace org —"
    echo "     External lets your personal Gmail sign in)"
    echo "  Contact information:"
    echo "  → Email address: your email address"
    echo "  Finish:"
    echo "  → Check the agreement box → 'CONTINUE & CREATE'"
    echo ""
    echo "  Next to change Scopes go to 'Data access' section:"
    echo "  → Click 'ADD OR REMOVE SCOPES'"
    echo "  → Add these scopes one at a time, or manually type them at the bottom:"
    echo "      https://www.googleapis.com/auth/drive.readonly"
    echo "      https://www.googleapis.com/auth/drive.file"
    echo "      https://www.googleapis.com/auth/spreadsheets.readonly"
    echo "  → Click 'UPDATE' → 'SAVE'"
    echo ""
    echo "  Next to add Test user go to 'Audience' section:"
    echo "  → Click 'ADD USERS' → enter your email → 'ADD'"
    echo "  This lets you sign in before Google verifies the app —"
    echo "     otherwise you'll get an 'unverified app' error."
    read -r -p "Press Enter when done…"

    # ── Step 5: Create OAuth client ID ──
    echo ""
    echo -e "${YELLOW}Step 5/6: Create the OAuth client ID${NC}"
    if command -v open &>/dev/null; then
        read -r -p "Open the Credentials page? [Y/n]: " resp
        if [ "$resp" != "n" ] && [ "$resp" != "N" ]; then
            open "https://console.cloud.google.com/apis/credentials" 2>/dev/null || true
        fi
    else
        echo "Go to: https://console.cloud.google.com/apis/credentials"
    fi
    echo ""
    echo "  → Make sure your project is selected (dropdown at the top)"
    echo "  → Click '+ CREATE CREDENTIALS' (top) → 'OAuth client ID'"
    echo "  → Application type: 'Desktop application'"
    echo "  → Name: 'scikick Desktop'"
    echo "  → Click 'CREATE'"
    echo "  → In the popup, click 'DOWNLOAD JSON'"
    read -r -p "Press Enter after downloading the JSON file…"

    # ── Step 6: Find and install the credentials ──
    echo ""
    echo -e "${YELLOW}Step 6/6: Installing your credentials${NC}"
    echo ""
    echo "  We need to copy the JSON file you just downloaded into the location"
    echo "  where scikick expects it:"
    echo -e "    ${GREEN}$CREDS_FILE${NC}"
    echo ""
    echo "  The file is named something like 'client_secret_XXXXXXXXXXXX.json'"
    echo "  and by default it lands in your ~/Downloads folder."
    echo ""

    FOUND=""
    DOWNLOADS="$HOME/Downloads"

    echo -n "  Scanning ~/Downloads for client_secret*.json… "

    if [ -d "$DOWNLOADS" ]; then
        if [ -r "$DOWNLOADS" ]; then
            CANDIDATES=$(ls -t "$DOWNLOADS"/client_secret*.json 2>/dev/null | head -3)
            if [ -n "$CANDIDATES" ]; then
                NEWEST=$(echo "$CANDIDATES" | head -1)
                echo "found!"
                echo "    $(basename "$NEWEST")"
                echo "    (downloaded: $(ls -lh "$NEWEST" | awk '{print $6, $7, $8}'))"
                FOUND="$NEWEST"
                echo "  Using this file automatically. (Run again to pick a different file.)"
                echo ""
            else
                echo "no client_secret*.json files found."
                echo ""
            fi
        else
            echo "cannot read ~/Downloads (permissions issue?)."
            echo "  Try: chmod +r ~/Downloads"
            echo ""
        fi
    else
        echo "~/Downloads not found."
        echo ""
    fi

    if [ -z "$FOUND" ]; then
        echo "  Let's find the file manually:"
        echo "    • Drag the file from your Downloads folder into this terminal"
        echo "    • Or paste the full path, e.g.:"
        echo "      $HOME/Downloads/client_secret_1234567890.json"
        echo ""
        read -r -p "  Path: " FOUND
        FOUND=$(echo "$FOUND" | sed "s/^['\"]//;s/['\"]\$//")
    fi

    if [ ! -f "$FOUND" ]; then
        echo -e "${RED}✗ File not found: $FOUND${NC}"
        echo ""
        echo "  Tip: the file is wherever your browser saved it — usually ~/Downloads."
        echo "  Look for a file named 'client_secret_*.json'."
        echo ""
        echo "  You can re-run this wizard later with: ./start.sh --setup"
        return 1
    fi

    # Copy the file to the app's credentials directory
    if ! cp "$FOUND" "$CREDS_FILE" 2>/dev/null; then
        echo ""
        echo -e "${RED}✗ Could not read the file (macOS may block terminal access to ~/Downloads).${NC}"
        echo ""
        echo "  Quick fix: drag the file to your Desktop, then paste the new path:"
        echo "    $HOME/Desktop/$(basename "$FOUND")"
        echo ""
        read -r -p "  New path: " FOUND
        FOUND=$(echo "$FOUND" | sed "s/^['\"]//;s/['\"]\$//")
        if [ ! -f "$FOUND" ]; then
            echo -e "${RED}✗ Still couldn't find the file.${NC}"
            echo "  Move it to your Desktop and re-run: ./start.sh --setup"
            return 1
        fi
        if ! cp "$FOUND" "$CREDS_FILE" 2>/dev/null; then
            echo -e "${RED}✗ Copy still failed.${NC}"
            echo "  Try manually: cp ~/Desktop/$(basename "$FOUND") $CREDS_FILE"
            echo "  Then re-run: ./start.sh --setup"
            return 1
        fi
    fi

    echo ""
    echo "  $(basename "$FOUND") → $CREDS_FILE"
    echo ""

    # ── Validate ──
    echo -n "Verifying the credentials file… "
    CLIENT_ID=$("${PYTHON3:-python3}" -c "
import json, sys
try:
    c = json.load(open('$CREDS_FILE'))
    inst = c.get('installed', c)
    cid = inst.get('client_id', '')
    secret = inst.get('client_secret', '')
    uri = inst.get('redirect_uris', [''])[0] if isinstance(inst.get('redirect_uris'), list) else ''
    if not cid or not secret:
        print('INVALID:missing_fields')
        sys.exit(1)
    if 'localhost' not in str(inst.get('redirect_uris', [])):
        print('OK_NOLOCAL')  # desktop app type — fine
    else:
        print('OK')
except Exception as e:
    print('INVALID:' + str(e))
    sys.exit(1)
" 2>/dev/null)

    if [[ "$CLIENT_ID" == INVALID* ]]; then
        echo -e "${RED}✗ Invalid${NC}"
        echo "  The file doesn't look like a valid OAuth client secret."
        echo "  Make sure you downloaded from 'OAuth 2.0 Client IDs' (not API keys or service accounts)."
        echo "  Error: ${CLIENT_ID#INVALID:}"
        rm -f "$CREDS_FILE"
        echo ""
        echo "You can re-run this wizard with: ./start.sh --setup"
        return 1
    fi

    echo -e "${GREEN}✓ Valid${NC}"
    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  Google Drive setup complete!${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  When you run './start.sh' for the first time, you'll be prompted"
    echo "  to authenticate with Google."
    echo ""
}

first_time_setup() {
    echo -e "${YELLOW}scikick Setup Wizard${NC}"
    echo ""

    # Check if LLM is already configured (has .env with API key)
    SKIP_LLM=false
    ENV_FILE="$SCRIPT_DIR/.env"
    if [ -f "$ENV_FILE" ] && grep -qE '^LLM_API_KEY=.+' "$ENV_FILE" 2>/dev/null; then
        source "$ENV_FILE"
        echo -e "${GREEN}✓ LLM already configured (${LLM_PROVIDER:-unknown} / ${LLM_MODEL:-default})${NC}"
        echo ""
        read -r -p "Reconfigure LLM? [y/N]: " resp
        if [ "$resp" != "y" ] && [ "$resp" != "Y" ]; then
            SKIP_LLM=true
            echo "  Skipping LLM setup — jumping to Google Drive."
            echo ""
        fi
    fi

    if [ "$SKIP_LLM" = false ]; then

    # --- Choose LLM provider ---
    echo -e "${YELLOW}Which LLM provider will you use?${NC}"
    echo "  1) Anthropic (Claude)  — https://console.anthropic.com/"
    echo "  2) DeepSeek             — https://platform.deepseek.com/"
    echo "  3) Zhipu AI (GLM)       — https://open.bigmodel.cn/"
    echo "  4) OpenAI (GPT-4o)      — https://platform.openai.com/"
    echo "  5) Custom (OpenAI-compatible — Ollama, Groq, Together, etc.)"
    echo ""
    read -r -p "Enter choice [1-5] (default: 1): " provider_choice
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
            LLM_PROVIDER="glm"
            DEFAULT_MODEL="glm-4-plus"
            echo -e "${GREEN}Selected: Zhipu AI (GLM)${NC}"
            echo "Get your API key at: https://open.bigmodel.cn/"
            ;;
        4)
            LLM_PROVIDER="openai"
            DEFAULT_MODEL="gpt-4o"
            echo -e "${GREEN}Selected: OpenAI${NC}"
            echo "Get your API key at: https://platform.openai.com/"
            ;;
        5)
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
    elif [ "$LLM_PROVIDER" = "glm" ]; then
        key_var="GLM_API_KEY"
        key_url="https://open.bigmodel.cn/"
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

    fi  # SKIP_LLM

    # Google Drive setup
    google_credentials_setup

    # Background service
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  Background Service${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  The scikick server needs to be running for the Chrome extension to work."
    echo ""
    echo "  Normally you'd run './start.sh' in a terminal each time you reboot"
    echo "  your computer. A background service skips that: it launches the server"
    echo "  automatically whenever you log into your macOS account (i.e., after"
    echo "  you turn on or restart your Mac and enter your password)."
    echo ""
    echo "  With this enabled, you just click the extension — no terminal needed."
    echo "  (Uses ~30 MB RAM when idle, localhost only, negligible CPU.)"
    echo ""
    read -r -p "  Install as background service? [Y/n]: " resp
    if [ "$resp" != "n" ] && [ "$resp" != "N" ]; then
        install_service
    else
        echo "  Skipped. You'll need to come back to this directory and run"
        echo "  './start.sh' manually each time you restart your Mac —"
        echo "  otherwise the extension won't be able to connect."
        echo ""
        echo "  You can always install the background service later with:"
        echo "    ./start.sh --install-service"
    fi

    echo ""
    echo -e "${GREEN}Setup complete!${NC}"
    echo "  Provider: $LLM_PROVIDER"
    echo "  Model: ${LLM_MODEL:-default}"
    echo ""
}

install_service() {
    PLIST="$HOME/Library/LaunchAgents/com.scikick.server.plist"
    LOG_FILE="$HOME/.scikick/server.log"
    ERR_FILE="$HOME/.scikick/server.err"

    echo ""
    echo -e "${BLUE}Installing background service…${NC}"
    echo "  This makes the server start automatically when you log into your Mac."
    echo "  No need to run ./start.sh manually — just click the extension."
    echo ""

    # macOS privacy protections block launchd from running scripts inside
    # Desktop and Documents folders. Warn before installing.
    case "$SCRIPT_DIR" in
        */Desktop/*|*/Documents/*)
            echo -e "${RED}⚠ WARNING: Your project is in a protected folder:${NC}"
            echo "  $SCRIPT_DIR"
            echo ""
            echo "  macOS blocks background services (launchd) from accessing"
            echo "  files in ~/Desktop and ~/Documents. The service will fail"
            echo "  with 'Operation not permitted' errors."
            echo ""
            echo "  To fix this, move the project to a different location first:"
            echo "    mv $SCRIPT_DIR ~/scikick"
            echo ""
            echo "  Then re-run this setup from the new location:"
            echo "    cd ~/scikick && ./start.sh --setup"
            echo ""
            read -r -p "  Continue anyway? [y/N]: " resp
            if [ "$resp" != "y" ] && [ "$resp" != "Y" ]; then
                echo "  Skipping background service."
                return 1
            fi
            ;;
    esac

    mkdir -p "$HOME/Library/LaunchAgents"
    mkdir -p "$HOME/.scikick"

    cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.scikick.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SCRIPT_DIR/start.sh</string>
        <string>--daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_FILE</string>
    <key>StandardErrorPath</key>
    <string>$ERR_FILE</string>
</dict>
</plist>
EOF

    # Stop any existing instance
    launchctl bootout gui/$(id -u)/com.scikick.server 2>/dev/null || true
    launchctl unload "$PLIST" 2>/dev/null || true

    # Load the service
    if launchctl bootstrap gui/$(id -u) "$PLIST" 2>/dev/null; then
        :
    elif launchctl load "$PLIST" 2>/dev/null; then
        :
    else
        echo -e "${RED}✗ Could not start the service automatically.${NC}"
        echo "  Try manually: launchctl load $PLIST"
        echo "  Or just run './start.sh' when you need the server."
        return 1
    fi

    # Verify it started
    sleep 1
    if curl -s http://localhost:8742/health > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Background service installed and running${NC}"
    else
        echo -e "${YELLOW}⚠ Service installed but may not have started.${NC}"
        echo "  Check logs: $ERR_FILE"
        echo "  Try manually: launchctl load $PLIST"
    fi
    echo "  Server starts automatically when you log in."
    echo "  Logs: $LOG_FILE"
    echo ""
    echo "  To uninstall: ./start.sh --uninstall-service"
    echo ""
}

uninstall_service() {
    PLIST="$HOME/Library/LaunchAgents/com.scikick.server.plist"

    echo "Stopping background service…"
    launchctl bootout gui/$(id -u)/com.scikick.server 2>/dev/null || \
    launchctl unload "$PLIST" 2>/dev/null || true

    rm -f "$PLIST"
    echo -e "${GREEN}✓ Background service removed${NC}"
    echo ""
}

start_server() {
    echo -e "${BLUE}Starting server...${NC}"

    # Check for .env file
    if [ -f "$SCRIPT_DIR/.env" ]; then
        source "$SCRIPT_DIR/.env"
    fi

    # Check for any LLM API key
    if [ -z "${LLM_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -z "${GLM_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
        echo -e "${RED}Error: No LLM API key found.${NC}"
        echo "Run './start.sh --setup' first, or set LLM_API_KEY in your shell."
        exit 1
    fi

    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  Server:  http://localhost:8742${NC}"
    echo -e "${GREEN}  Health:  http://localhost:8742/health${NC}"
    echo -e "${GREEN}  API docs: http://localhost:8742/docs${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    # ── First-run onboarding (skip if already set up) ──
    TOKEN_FILE="$HOME/.scikick/google_token.json"
    if [ ! -f "$TOKEN_FILE" ]; then
        # Extension loading guide (first time only)
        if [ -f "$SCRIPT_DIR/install-extension.sh" ]; then
            bash "$SCRIPT_DIR/install-extension.sh"
        else
            echo -e "${YELLOW}Load the Chrome extension:${NC}"
            echo "  → Go to chrome://extensions/"
            echo "  → Enable 'Developer mode' (toggle in top right)"
            echo "  → Click 'Load unpacked'"
            echo "  → Select: $SCRIPT_DIR/extension"
            echo ""
        fi

        # Wait for user to complete setup before starting server
        echo ""
        read -r -p "Once you've completed the steps above, type 'done' to start the server: " resp
        while [ "$resp" != "done" ]; do
            read -r -p "Type 'done' when ready: " resp
        done
        echo ""
    fi

    echo -e "${YELLOW}Press Ctrl+C to stop the server${NC}"
    echo ""

    # Check if port is already in use.
    # `lsof` exits 1 when the port is FREE; the `|| true` keeps `set -e` from
    # aborting the script on a free port (it would otherwise silently exit
    # right here, before reaching `python3 main.py`).
    local port_pid
    port_pid=$(lsof -ti :8742 2>/dev/null || true)
    if [ -n "$port_pid" ]; then
        echo -e "${RED}Port 8742 is already in use.${NC}"
        echo ""

        # Check if the background service is the culprit
        local plist="$HOME/Library/LaunchAgents/com.scikick.server.plist"
        if [ -f "$plist" ] && launchctl list com.scikick.server &>/dev/null; then
            echo "  The background service is running and will auto-restart"
            echo "  if you just kill the process. Stop it first:"
            echo ""
            echo -e "    ${GREEN}./start.sh --uninstall-service${NC}"
            echo ""
            echo "  Or to just stop it temporarily:"
            echo -e "    ${GREEN}launchctl bootout gui/\$(id -u)/com.scikick.server${NC}"
            echo ""
        else
            echo "  To free the port, run:"
            echo -e "    ${GREEN}lsof -ti :8742 | xargs kill${NC}"
            echo ""
        fi
        exit 1
    fi

    cd "$SERVER_DIR"
    "$VENV_DIR/bin/python3" main.py
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
    --install-service)
        install_service
        ;;
    --uninstall-service)
        uninstall_service
        ;;
    --daemon)
        # Silent mode for launchd — skip banner and interactive prompts
        if [ -f "$SCRIPT_DIR/.env" ]; then
            set -a; source "$SCRIPT_DIR/.env"; set +a
        fi
        if [ ! -d "$VENV_DIR" ]; then
            install_deps
        fi
        cd "$SERVER_DIR"
        exec "$VENV_DIR/bin/python3" main.py
        ;;
    --help|-h)
        echo "Usage: ./start.sh [OPTION]"
        echo ""
        echo "Options:"
        echo "  (none)             Start the server"
        echo "  --install          Install dependencies, then start"
        echo "  --setup            Setup wizard (LLM + Google Drive)"
        echo "  --install-service  Install as background service (auto-start on login)"
        echo "  --uninstall-service Remove background service"
        echo "  --help             Show this help"
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
