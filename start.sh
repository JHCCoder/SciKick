#!/usr/bin/env bash
# scikick — One-command launcher
#
# Usage:
#   ./start.sh                    # Start the server
#   ./start.sh --install          # Install dependencies first, then start
#   ./start.sh --setup            # First-time setup wizard (Google Drive + service)
#
# Requirements:
#   - Python 3.10+
#   - Chrome/Chromium browser (for the extension)
#   - Google Cloud project with Drive API enabled (for Google Drive access)
#
# The LLM API key is NOT required up front — the server boots keyless and you
# configure your provider/key in the extension's ⚙ Settings panel on first run.

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
    conda_python=$(command -v conda 2>/dev/null || true)

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
" 2>/dev/null || true)

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
            CANDIDATES=$(ls -t "$DOWNLOADS"/client_secret*.json 2>/dev/null | head -3 || true)
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
" 2>/dev/null || true)

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
    echo "  This wizard sets up Google Drive access and the optional background"
    echo "  service. Your LLM provider and API key are configured later in the"
    echo "  extension's ⚙ Settings panel — no terminal needed."
    echo ""

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
    echo "  Next: open the SciKick side panel and click ⚙ Settings to enter"
    echo "  your LLM provider and API key."
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

# PIDs of processes actively LISTENing on port 8742 (empty if none).
#
# We filter to the LISTEN state on purpose: a plain `lsof -ti :8742` also
# matches stale *client-side* sockets (CLOSE_WAIT / TIME_WAIT left behind in
# e.g. Chrome after the previous server session ended). Those do NOT prevent
# binding a new listener — only an actual LISTEN socket does — but they made
# start.sh refuse to launch with a false "port already in use" and tell the
# user to kill Chrome, which was wrong. LISTEN-only detection ignores them.
_port_listen_pids() {
    lsof -nP -tiTCP:8742 -sTCP:LISTEN 2>/dev/null || true
}

start_server() {
    echo -e "${BLUE}Starting server...${NC}"

    # Check for .env file
    if [ -f "$SCRIPT_DIR/.env" ]; then
        source "$SCRIPT_DIR/.env"
    fi

    # The server boots keyless — the LLM API key is configured in the
    # extension's ⚙ Settings panel, not here. If no key is set yet, the side
    # panel shows a nudge on first run; no need to block startup.
    if [ -z "${LLM_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -z "${GLM_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${MOONSHOT_API_KEY:-}" ]; then
        echo -e "${YELLOW}No LLM API key found yet — that's fine.${NC}"
        echo "  Open the SciKick side panel and click ⚙ Settings to enter your key."
        echo ""
    fi

    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  Server:  http://localhost:8742${NC}"
    echo -e "${GREEN}  Health:  http://localhost:8742/health${NC}"
    echo -e "${GREEN}  API docs: http://localhost:8742/docs${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    # No first-run gate here: the server boots keyless, and Google Drive
    # access is added on demand via './start.sh --drive' (the side panel
    # prompts for it when the user opens a Drive folder). Web Store users
    # already have the extension, so the old "load unpacked / type done"
    # onboarding is gone.

    echo -e "${YELLOW}Press Ctrl+C to stop the server${NC}"
    echo ""

    # Check if port is already in use — but only count a real LISTEN socket
    # (see _port_listen_pids: stale client-side CLOSE_WAIT sockets from e.g.
    # Chrome don't actually hold the port and shouldn't block startup).
    # `lsof` exits 1 when nothing is listening; the helper's `|| true` keeps
    # `set -e` from aborting the script on a free port (it would otherwise
    # silently exit right here, before reaching `python3 main.py`).
    local port_pid
    port_pid=$(_port_listen_pids)
    if [ -n "$port_pid" ]; then
        if ! resolve_port_conflict; then
            exit 1
        fi
    fi

    cd "$SERVER_DIR"
    "$VENV_DIR/bin/python3" main.py
}

# Resolve a "port 8742 already in use" situation interactively.
#
# Inspects whatever is holding the port and offers the right action:
#   - launchd background service → must stop the service (else it respawns)
#   - suspended scikick (Ctrl+Z) → SIGCONT+SIGTERM (graceful) or kill -9
#   - running scikick             → SIGTERM (graceful) or kill -9
#   - foreign (non-scikick) app   → warn, don't auto-kill
# Returns 0 if the port is now free, 1 otherwise (caller aborts).
resolve_port_conflict() {
    local pids
    pids=$(_port_listen_pids)
    [ -z "$pids" ] && return 0   # race: freed already

    echo -e "${YELLOW}Port 8742 is already in use.${NC}"
    echo ""

    # ── launchd background service? Killing the process just respawns it. ──
    local plist="$HOME/Library/LaunchAgents/com.scikick.server.plist"
    if [ -f "$plist" ] && launchctl list com.scikick.server &>/dev/null; then
        echo -e "${YELLOW}The scikick background service is managing port 8742.${NC}"
        echo "  It auto-restarts if you kill the process, so stop the service first:"
        echo ""
        echo -e "    ${GREEN}./start.sh --uninstall-service${NC}"
        echo "  Or pause it temporarily:"
        echo -e "    ${GREEN}launchctl bootout gui/\$(id -u)/com.scikick.server${NC}"
        echo ""
        return 1
    fi

    # ── Classify each holder: state + whether it's scikick. ──
    # ps stat: T = stopped (suspended via Ctrl+Z), S/R = running, Z = zombie.
    local stopped_pids="" running_pids="" foreign_pids=""
    local pid stat cmd is_scikick
    for pid in $pids; do
        stat=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ' || true)
        cmd=$(ps -o command= -p "$pid" 2>/dev/null || true)
        if echo "$cmd" | grep -qiE "main\.py|scikick|start\.sh"; then
            is_scikick=1
        else
            is_scikick=0
        fi

        if [ "$is_scikick" = "0" ]; then
            foreign_pids="$foreign_pids $pid"
            echo -e "${YELLOW}  (also held by non-scikick process PID $pid:${NC} ${cmd:0:70}${YELLOW})${NC}"
            continue
        fi

        case "$stat" in
            T*) stopped_pids="$stopped_pids $pid" ;;
            *)  running_pids="$running_pids $pid" ;;
        esac
    done

    # ── Foreign-only holder → don't touch it. ──
    if [ -n "$foreign_pids" ] && [ -z "$stopped_pids" ] && [ -z "$running_pids" ]; then
        echo -e "${RED}Port 8742 is held by another application (not scikick).${NC}"
        echo "  I won't kill it automatically. Check what it is and stop it manually:"
        echo -e "    ${GREEN}lsof -i :8742${NC}"
        echo "  Then re-run ./start.sh."
        echo ""
        return 1
    fi

    # ── Suspended scikick (Ctrl+Z): plain kill won't work. ──
    if [ -n "$stopped_pids" ]; then
        echo -e "${YELLOW}A scikick server is suspended (you pressed Ctrl+Z) and is still${NC}"
        echo -e "${YELLOW}holding port 8742. A plain 'kill' won't stop a suspended process.${NC}"
        echo "  PID(s):$(echo "$stopped_pids" | sed 's/^ *//;s/  */, /g')"
        echo ""
        echo "  g) Resume & stop gracefully — SIGCONT + SIGTERM (flushes memory to Drive)"
        echo "  k) Kill it now — kill -9 (fast, skips the on-close memory flush)"
        echo "  c) Cancel"
        read -r -p "  Choose [g/k/c] (default: g): " resp
        resp="${resp:-g}"
        case "$resp" in
            g|G)
                # Resume (SIGCONT) so the pending SIGTERM can take effect, then
                # SIGTERM → uvicorn graceful shutdown → lifespan flush.
                kill -CONT $stopped_pids 2>/dev/null || true
                sleep 0.3
                kill -TERM $stopped_pids 2>/dev/null || true
                echo -e "${BLUE}Resumed + sent SIGTERM — waiting for graceful shutdown…${NC}"
                if ! _wait_port_free 20; then
                    echo -e "${YELLOW}Still holding the port after 20s — force killing.${NC}"
                    kill -9 $stopped_pids 2>/dev/null || true
                    sleep 1
                fi
                ;;
            k|K)
                kill -9 $stopped_pids 2>/dev/null || true
                echo -e "${GREEN}Killed suspended server.${NC}"
                sleep 1
                ;;
            *)
                echo "Cancelled."
                return 1
                ;;
        esac
        # The uvicorn reloader parent (python3 main.py with reload=True) is
        # also suspended and isn't the port holder, so it lingers after we
        # signal its child. Nudge the user to clean it up in their terminal.
        echo -e "${YELLOW}Note: the suspended launcher may still linger in the terminal${NC}"
        echo -e "${YELLOW}where you pressed Ctrl+Z. Clean it up there with:${NC}"
        echo -e "    ${GREEN}fg %1   (then Ctrl+C)${NC}  — or —  ${GREEN}kill -9 %1${NC}"
        echo ""
    fi

    # ── Running scikick: graceful SIGTERM (uvicorn shuts down + flushes). ──
    if [ -n "$running_pids" ]; then
        echo -e "${YELLOW}A scikick server is already running (PID$(echo "$running_pids" | sed 's/^ *//;s/  */, /')).${NC}"
        echo "  Stopping it gracefully lets it flush memory to Drive before exiting."
        echo ""
        echo "  s) Stop it gracefully — SIGTERM (recommended)"
        echo "  k) Kill -9 (immediate, no flush)"
        echo "  c) Cancel"
        read -r -p "  Choose [s/k/c] (default: s): " resp
        resp="${resp:-s}"
        case "$resp" in
            s|S)
                kill -TERM $running_pids 2>/dev/null || true
                echo -e "${BLUE}Sent SIGTERM — waiting for graceful shutdown…${NC}"
                if ! _wait_port_free 20; then
                    echo -e "${YELLOW}Still running after 20s — force killing.${NC}"
                    kill -9 $running_pids 2>/dev/null || true
                    sleep 1
                fi
                ;;
            k|K)
                kill -9 $running_pids 2>/dev/null || true
                echo -e "${GREEN}Killed running server.${NC}"
                sleep 1
                ;;
            *)
                echo "Cancelled."
                return 1
                ;;
        esac
    fi

    # ── Final check. ──
    if [ -n "$(_port_listen_pids)" ]; then
        echo -e "${RED}Port 8742 is still in use.${NC}"
        echo "  Free it manually, then re-run ./start.sh:"
        echo -e "    ${GREEN}lsof -ti :8742 -sTCP:LISTEN | xargs kill -9${NC}"
        echo ""
        return 1
    fi
    echo -e "${GREEN}✓ Port 8742 is free.${NC}"
    echo ""
    return 0
}

# Wait up to $1 seconds for port 8742 to be freed. Returns 0 if freed, 1 on timeout.
_wait_port_free() {
    local max="$1" waited=0
    while [ "$waited" -lt "$max" ]; do
        sleep 1
        waited=$((waited + 1))
        if [ -z "$(_port_listen_pids)" ]; then
            echo -e "${GREEN}Server stopped and port freed.${NC}"
            return 0
        fi
    done
    return 1
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
    --drive)
        # Add Google Drive access on demand — runnable while the server is up.
        # Only places the OAuth client-secrets file (~/.scikick/google_credentials.json);
        # the OAuth consent itself is triggered from the extension via /drive/auth/url.
        google_credentials_setup && \
          echo -e "${GREEN}Done. Return to the SciKick side panel and click 'Connect Google Drive'.${NC}"
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
        echo "  --setup            Setup wizard (Google Drive + background service)"
        echo "  --drive            Add Google Drive access (runnable while the server runs)"
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
