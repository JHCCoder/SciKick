/**
 * scikick — Side Panel Chat UI
 * Connects to the local server at localhost:8742
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SERVER_URL = "http://localhost:8742";
const POLL_INTERVAL_MS = 5000; // Health check polling
const HEALTH_FAIL_THRESHOLD = 2; // Consecutive failures before showing disconnected

// Default Base URLs for local LLM runtimes — prefilled when the user picks
// one in the dropdown. Mirrors PROVIDER_DEFAULTS in server/config.py.
const LOCAL_DEFAULTS = {
  "local-ollama": "http://localhost:11434/v1",
  "local-lmstudio": "http://localhost:1234/v1",
  "local-mlx": "http://localhost:8080/v1",
};

// Split a provider's comma-separated model list from /chat/providers into an
// array. Real model IDs never contain spaces, so drop parenthetical hints and
// any descriptive phrase (e.g. local runtimes append "… (whatever you `ollama
// pull`ed)" or "whatever model is loaded…"). `custom` has no fixed list and is
// handled separately (free-text model entry).
function parseModelList(models) {
  return (models || "")
    .split(",")
    .map((s) => s.split("(")[0].trim()) // drop parenthetical hints
    .filter((s) => s && !/\s/.test(s));  // a real model ID has no spaces
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let serverConnected = false;
let everConnected = false; // has the panel ever reached the server? (drives first-run vs reconnect banner)
let projectLoaded = false;
let projectFiles = []; // {id, name, mimeType} — files in the loaded project
let projectFolderId = null; // Drive folder ID of the loaded project
let providerModels = {}; // provider id -> [model ids], from /chat/providers
let viewingFile = null; // {name, id} — project file currently open in a browser tab
let currentTabUrl = null; // URL of the currently active browser tab
let currentDriveFolderId = null; // Drive folder id on the active tab, if loadable (else null)
let driveReady = false; // last known /drive/auth/status authenticated value
let sessionFocus = null; // "brainstorming" | "paper_discussion" | "paper_writing" | "revision" | "other"
let currentStream = null; // AbortController for SSE
let loadingInProgress = false; // true during loadProject / scrape — suppress disconnect banner
let generating = false; // true while an LLM response is streaming — locks input + scan (ChatGPT-style)
let contextUpdating = false; // true while a blocking context operation (Update / Scrape / Load project) is in flight — locks chat + context controls
let healthFailCount = 0; // consecutive health check failures (prevents false disconnect flash)
let bgPort = null; // Port to background service worker (keep-alive only)
let thinkingMode = "auto"; // "auto" | "on" | "off" — DeepSeek v4 chain-of-thought
let currentProvider = null; // last known provider/model — sent with the thinking toggle
let currentModel = null; //  so /chat/configure keeps the runtime overrides intact

// ---------------------------------------------------------------------------
// DOM Elements
// ---------------------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const dom = {
  statusDot: $("#status-dot"),
  projectName: $("#project-name"),
  connectBanner: $("#connect-banner"),
  projectBar: $("#project-bar"),
  driveInput: $("#drive-folder-input"),
  btnLoad: $("#btn-load-project"),
  messages: $("#messages"),
  contextHint: $("#context-hint"),
  chatInput: $("#chat-input"),
  btnSend: $("#btn-send"),
  btnStop: $("#btn-stop"),
  btnTheme: $("#btn-theme"),
  btnClear: $("#btn-clear"),
  btnPower: $("#btn-power"),
  btnConnect: $("#btn-connect"),
  firstrunInstall: $("#firstrun-install"),
  firstrunReconnect: $("#firstrun-reconnect"),
  // Drive setup banner
  driveSetupBanner: $("#drive-setup-banner"),
  btnDriveConnect: $("#btn-drive-connect"),
  btnDriveRecheck: $("#btn-drive-recheck"),
  // Settings
  btnSettings: $("#btn-settings"),
  settingsPanel: $("#settings-panel"),
  btnSettingsClose: $("#btn-settings-close"),
  btnSettingsSave: $("#btn-settings-save"),
  cfgProvider: $("#cfg-provider"),
  cfgApiKey: $("#cfg-api-key"),
  cfgApiKeyLabel: $("#cfg-api-key-label"),
  cfgModel: $("#cfg-model"), // native select — providers with a known model list
  cfgModelText: $("#cfg-model-text"), // free-text — custom / local runtimes
  cfgBaseUrl: $("#cfg-base-url"),
  cfgBaseUrlLabel: $("#cfg-base-url-label"),
  cfgStatus: $("#cfg-status"),
  pdfCapStatus: $("#pdf-cap-status"),
  // Info panel
  btnInfo: $("#btn-info"),
  infoPanel: $("#info-panel"),
  btnInfoClose: $("#btn-info-close"),
  infoBody: $("#info-body"),
  // Search panel
  btnSearch: $("#btn-search"),
  searchPanel: $("#search-panel"),
  btnSearchClose: $("#btn-search-close"),
  searchInput: $("#search-input"),
  searchResults: $("#search-results"),
  // Pin panel
  btnPin: $("#btn-pin"),
  pinPanel: $("#pin-panel"),
  btnPinClose: $("#btn-pin-close"),
  pinList: $("#pin-list"),
  pinCount: $("#pin-count"),
  // Context menu
  ctxMenu: $("#ctx-menu"),
  ctxMenuPin: $("#ctx-menu-pin"),
  // Context window
  ctxBar: $("#context-usage-bar"),
  ctxFill: $("#ctx-fill-bar"),
  ctxStats: $("#ctx-stats"),
  btnRefreshCtx: $("#btn-refresh-ctx"),
  // Current tab
  tabBar: $("#current-tab-bar"),
  tabIcon: $("#current-tab-bar .tab-icon"),
  tabTitle: $("#current-tab-title"),
  tabDomain: $("#current-tab-domain"),
  btnUseTab: $("#btn-use-tab"),
  btnScanTab: $("#btn-scan-tab"),
};

// ---------------------------------------------------------------------------
// Server Connection
// ---------------------------------------------------------------------------

async function checkServerHealth() {
  try {
    const res = await fetch(`${SERVER_URL}/health`, {
      method: "GET",
      signal: AbortSignal.timeout(3000),
    });
    if (res.ok) {
      const data = await res.json();
      if (data.status === "ok") {
        healthFailCount = 0;
        setServerStatus("connected");
        return true;
      }
    }
  } catch (e) {
    // Server not reachable — could be transient (Drive sync, LLM streaming, etc.)
  }

  // Require HEALTH_FAIL_THRESHOLD consecutive failures before showing disconnected.
  // This prevents false "backend not connected" flashes when the server is
  // momentarily busy with Drive uploads or LLM API calls.
  healthFailCount++;
  if (healthFailCount >= HEALTH_FAIL_THRESHOLD) {
    setServerStatus("disconnected");
  }
  return false;
}

function setServerStatus(status) {
  // Don't flash the "disconnected" banner while a long-running
  // operation (project load, scrape, memory sync) is in progress —
  // the server is busy, not down.
  if (status === "disconnected" && loadingInProgress) return;

  const wasConnected = serverConnected;
  serverConnected = status === "connected";
  dom.statusDot.className = `status-${status}`;
  dom.statusDot.title = `Server: ${status}`;

  if (serverConnected) {
    dom.connectBanner.style.display = "none";
    dom.driveInput.disabled = false;
    dom.btnLoad.disabled = !dom.driveInput.value.trim();
    // Enable chat regardless of whether a project is loaded (unless a
    // response is currently streaming — see applyInputState).
    applyInputState();

    // First successful contact — remember it so future disconnects show the
    // "reconnect" banner (service hint) instead of the first-run "clone +
    // ./start.sh" banner.
    if (!everConnected) {
      everConnected = true;
      chrome.storage.local.set({ everConnected: true });
    }

    // Server just came back — in-memory state was wiped on restart.
    // Reset client-side project state and refresh the info panel.
    if (!wasConnected) {
      projectLoaded = false;
      projectFiles = [];
      projectFolderId = null;
      viewingFile = null;
      dom.projectName.textContent = "SciKick";
      if (!dom.infoPanel.classList.contains("hidden")) {
        loadInfoPanel();
      }
      // Re-check Drive readiness for the current tab now that the server is up.
      checkDriveStatus();
    }
  } else {
    dom.connectBanner.style.display = "block";
    // First-run (never connected): show clone + ./start.sh. Otherwise the
    // shorter reconnect hint (background service / Desktop-Documents caveat).
    dom.firstrunInstall.classList.toggle("hidden", everConnected);
    dom.firstrunReconnect.classList.toggle("hidden", !everConnected);
    dom.driveInput.disabled = true;
    dom.btnLoad.disabled = true;
    applyInputState();
    // No server → can't check Drive status; hide the Drive setup banner.
    dom.driveSetupBanner.style.display = "none";

    // Server went down — update info panel if open
    if (!dom.infoPanel.classList.contains("hidden")) {
      dom.infoBody.innerHTML = '<div class="info-empty">Server disconnected. Data will reload when reconnected.</div>';
    }
  }
}

async function connect() {
  setServerStatus("connecting");
  const ok = await checkServerHealth();
  if (ok) {
    // Show active provider
    await showProviderInfo();
    // Detect PDF parsing tiers (Fast / Auto / Deep) for the Settings status line.
    checkPdfCapabilities();
    // Onboarding (generic brainstorming picker vs. project-goal onboarding) is
    // decided by checkExistingSession below, once we know whether a project
    // is active — so we don't double-prompt.
    // Check for existing session
    await checkExistingSession();
    // Re-check Drive readiness now that the server is up (the current tab
    // may be a Drive folder waiting on the setup banner).
    checkDriveStatus();
  }
}

// --- Contextual Google Drive onboarding ---
// When the active tab is a Drive folder but Drive isn't ready, show a banner
// telling the user how to fix it: ./start.sh --drive if the credentials file
// is missing, or a "Connect Google Drive" button (browser OAuth) if only the
// token is missing. Once authenticated, the usual "Use this folder" button
// appears.
async function checkDriveStatus() {
  if (!serverConnected) {
    dom.driveSetupBanner.style.display = "none";
    return;
  }
  try {
    const res = await fetch(`${SERVER_URL}/drive/auth/status`);
    if (!res.ok) return;
    const data = await res.json();
    driveReady = !!data.authenticated;
    applyDriveStatus(data);
  } catch {
    // Transient network blip — leave the banner as-is.
  }
}

function applyDriveStatus(data) {
  // Only prompt when actually on a loadable Drive folder.
  if (!currentDriveFolderId) {
    dom.driveSetupBanner.style.display = "none";
    return;
  }
  if (data.authenticated) {
    dom.driveSetupBanner.style.display = "none";
    dom.btnUseTab.classList.remove("hidden");
    dom.btnUseTab.textContent = "Use this folder";
    return;
  }
  // Not ready — show the banner, hide the Load button.
  dom.btnUseTab.classList.add("hidden");
  dom.driveSetupBanner.style.display = "block";
  const credsMissing = !data.credentials_present;
  document.querySelector('.ds-state[data-state="creds"]')
    .classList.toggle("hidden", !credsMissing);
  document.querySelector('.ds-state[data-state="token"]')
    .classList.toggle("hidden", credsMissing);
  // The "Connect Google Drive" button only applies to the token-missing state.
  dom.btnDriveConnect.classList.toggle("hidden", credsMissing);
}

async function showProviderInfo() {
  try {
    const res = await fetch(`${SERVER_URL}/chat/providers`);
    if (res.ok) {
      const data = await res.json();
      if (data.current && data.current.configured) {
        showProviderHint(
          data.current.provider, data.current.model, data.current.thinking_mode,
          data.current.thinking_capable
        );
      } else {
        // No LLM configured yet — nudge the user to ⚙ Settings instead of
        // letting them discover it via a failed first chat.
        showConfigNudge();
      }
    }
  } catch (e) {
    // Provider info not critical
  }
}

// Cached PDF parsing capabilities (Fast / Auto / Deep). Polled once after the
// server connects; the Settings panel renders a status line and an install
// hint when the Auto (scanned-page OCR) tier isn't installed.
let pdfCaps = null;

async function checkPdfCapabilities() {
  if (!serverConnected) return;
  try {
    const res = await fetch(`${SERVER_URL}/chat/pdf-capabilities`);
    if (!res.ok) return;
    pdfCaps = await res.json();
    renderPdfCapStatus();
  } catch {
    // Non-critical — leave the status line as-is.
  }
}

function renderPdfCapStatus() {
  if (!dom.pdfCapStatus || !pdfCaps) return;
  const fast = pdfCaps.fast ? "Fast ✓" : "Fast ✗";
  const auto = pdfCaps.auto ? "Auto ✓" : "Auto ✗";
  const deep = pdfCaps.deep ? "Deep ✓" : "Deep —";
  let line = `PDF parsing: ${fast} · ${auto} · ${deep}`;
  if (!pdfCaps.auto && pdfCaps.install_hint) {
    // Scanned-page OCR not installed — show the install command so the user
    // knows exactly how to enable it (mirrors the Drive-setup gate pattern).
    line += `\n${pdfCaps.install_hint}`;
  }
  dom.pdfCapStatus.textContent = line;
  dom.pdfCapStatus.classList.toggle("pdf-cap-missing", !pdfCaps.auto);
}

function showProviderHint(provider, model, thinking, capable) {
  currentProvider = provider;
  currentModel = model;
  if (thinking) thinkingMode = thinking;
  // Show just the connected model — the provider label (esp. "Others")
  // isn't useful in the status bar; the model name is what matters. The
  // Auto/On/Off chain-of-thought toggle rides along in the same bar, but
  // only for models that can actually toggle thinking (reasoning families).
  const toggle = capable
    ? `<span class="ctx-thinking">` +
        `<button class="tk-opt${thinkingMode === "auto" ? " active" : ""}" data-mode="auto" title="Auto — think for substantive questions, skip trivial ones">Auto</button>` +
        `<button class="tk-opt${thinkingMode === "on" ? " active" : ""}" data-mode="on" title="Always think (chain-of-thought)">On</button>` +
        `<button class="tk-opt${thinkingMode === "off" ? " active" : ""}" data-mode="off" title="Never think — fastest replies">Off</button>` +
      `</span>`
    : "";
  dom.contextHint.innerHTML =
    `<span class="ctx-model">🧠 <strong>${escHtml(model)}</strong></span>` + toggle;
  dom.contextHint.classList.remove("hidden", "config-nudge");
  dom.contextHint.onclick = null;
}

function renderThinkingToggle() {
  // Update the active segment after a toggle click (no full re-render needed).
  document.querySelectorAll(".ctx-thinking .tk-opt").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === thinkingMode);
  });
}

async function setThinkingMode(mode) {
  if (!["auto", "on", "off"].includes(mode) || mode === thinkingMode) return;
  thinkingMode = mode;
  renderThinkingToggle();
  try {
    // Send the current provider/model along so the server keeps its runtime
    // overrides (it resets them on each /configure call).
    await fetch(`${SERVER_URL}/chat/configure`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: currentProvider || undefined,
        model: currentModel || undefined,
        thinking_mode: mode,
        persist: true,
      }),
    });
  } catch (e) {
    // Non-critical — the mode resets on the next provider-info refresh.
  }
}

function showConfigNudge() {
  dom.contextHint.innerHTML = `⚙ <strong>No LLM configured</strong> — click to enter your API key`;
  dom.contextHint.classList.remove("hidden");
  dom.contextHint.classList.add("config-nudge");
  dom.contextHint.onclick = openSettings;
}

// ---------------------------------------------------------------------------
// Settings Panel
// ---------------------------------------------------------------------------

async function openSettings() {
  // Toggle: if already open, close it
  if (!dom.settingsPanel.classList.contains("hidden")) {
    closeSettings();
    return;
  }

  // Populate with current values from server
  try {
    const res = await fetch(`${SERVER_URL}/chat/providers`);
    if (res.ok) {
      const data = await res.json();
      // Cache each provider's model list for the model dropdown. `custom` has
      // no fixed list (model is free text); local runtimes depend on what the
      // user has loaded, so their hint lists are offered as suggestions only.
      providerModels = {};
      for (const p of data.available || []) {
        if (p.id === "custom") continue;
        providerModels[p.id] = parseModelList(p.models);
      }
      if (data.current) {
        dom.cfgProvider.value = data.current.provider || "anthropic";
        const model = data.current.model || "";
        dom.cfgModel.value = model;
        dom.cfgModelText.value = model;
      }
    }
  } catch (e) {
    // Use defaults
  }
  dom.cfgApiKey.value = ""; // never pre-fill the key
  dom.settingsPanel.classList.remove("hidden");
  handleProviderChange(false);
}

function closeSettings() {
  dom.settingsPanel.classList.add("hidden");
  dom.cfgStatus.textContent = "";
  dom.cfgStatus.className = "";
}

function handleProviderChange(prefill = true) {
  const provider = dom.cfgProvider.value;
  const isLocal = Object.prototype.hasOwnProperty.call(LOCAL_DEFAULTS, provider);
  const showsBaseUrl = provider === "custom" || isLocal;

  // Model — a native <select> when the provider has a known model list,
  // swapping to a free-text input for providers whose model depends on the
  // runtime (custom / local). Both share identical styling, so the swap is
  // seamless and matches the provider dropdown above.
  const modelOptions = providerModels[provider] || [];
  const visibleModel = dom.cfgModel.classList.contains("hidden")
    ? dom.cfgModelText.value : dom.cfgModel.value;
  const modelValue = (visibleModel || "").trim();
  if (!provider) {
    // No provider chosen yet — lock the model field until one is picked.
    dom.cfgModel.disabled = true;
    dom.cfgModel.innerHTML =
      '<option value="" disabled selected>Choose a provider first</option>';
    dom.cfgModel.value = "";
    dom.cfgModel.classList.remove("hidden");
    dom.cfgModelText.classList.add("hidden");
  } else if (modelOptions.length) {
    dom.cfgModel.disabled = false;
    dom.cfgModel.innerHTML = modelOptions
      .map((m) => `<option value="${m}">${m}</option>`)
      .join("");
    // Keep the current model when it's in the list; otherwise default to the
    // provider's first model so the field never shows a stale value.
    dom.cfgModel.value = modelOptions.includes(modelValue) ? modelValue : modelOptions[0];
    dom.cfgModel.classList.remove("hidden");
    dom.cfgModelText.classList.add("hidden");
  } else {
    dom.cfgModel.disabled = false;
    // Custom / local — preserve whatever was typed so nothing is lost.
    dom.cfgModelText.value = modelValue;
    dom.cfgModelText.placeholder = isLocal
      ? "model name loaded in your runtime"
      : "any model name your provider supports";
    dom.cfgModel.classList.add("hidden");
    dom.cfgModelText.classList.remove("hidden");
  }

  // Base URL field — shown for custom + local runtimes.
  dom.cfgBaseUrl.classList.toggle("hidden", !showsBaseUrl);
  dom.cfgBaseUrlLabel.classList.toggle("hidden", !showsBaseUrl);

  // Prefill the local runtime's default port when the field is empty so the
  // user sees what endpoint will be used (still editable). Only on
  // user-initiated changes — on settings-open we leave the field empty so a
  // previously saved custom port isn't clobbered (an empty field sends no
  // base_url, so the server keeps its existing value).
  if (prefill && isLocal && !dom.cfgBaseUrl.value.trim()) {
    dom.cfgBaseUrl.value = LOCAL_DEFAULTS[provider];
  }

  // API Key — local runtimes don't use one; hide the field + label.
  dom.cfgApiKey.classList.toggle("hidden", isLocal);
  dom.cfgApiKeyLabel.classList.toggle("hidden", isLocal);
}

async function saveSettings() {
  const provider = dom.cfgProvider.value;
  const apiKey = dom.cfgApiKey.value.trim();
  const modelEl = dom.cfgModel.classList.contains("hidden") ? dom.cfgModelText : dom.cfgModel;
  const model = modelEl.value.trim();
  const baseUrl = dom.cfgBaseUrl.value.trim();

  if (!provider) return;

  dom.btnSettingsSave.disabled = true;
  dom.cfgStatus.textContent = "Saving...";
  dom.cfgStatus.className = "";

  try {
    const res = await fetch(`${SERVER_URL}/chat/configure`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider,
        api_key: apiKey || undefined,
        model: model || undefined,
        base_url: baseUrl || undefined,
        persist: true,
      }),
    });

    if (res.ok) {
      const data = await res.json();
      dom.cfgStatus.textContent = "✓ Applied";
      dom.cfgStatus.className = "";
      // Update the context hint (clears the config nudge if it was shown)
      if (data.current) {
        showProviderHint(
          data.current.provider, data.current.model, data.current.thinking_mode,
          data.current.thinking_capable
        );
      }
      // Clear the API key field for security
      dom.cfgApiKey.value = "";
      // Close after short delay so user sees success
      setTimeout(closeSettings, 1200);
    } else {
      const err = await res.json().catch(() => ({ detail: "Unknown error" }));
      dom.cfgStatus.textContent = `✗ ${err.detail || "Failed"}`;
      dom.cfgStatus.className = "error";
    }
  } catch (e) {
    dom.cfgStatus.textContent = `✗ ${e.message}`;
    dom.cfgStatus.className = "error";
  } finally {
    dom.btnSettingsSave.disabled = false;
  }
}

async function checkExistingSession() {
  try {
    const res = await fetch(`${SERVER_URL}/memory/status`);
    if (res.ok) {
      const data = await res.json();
      if (data.active && data.memory) {
        const mem = data.memory;
        showSystemMessage(
          `📋 **Resumed session** from ${mem.last_computer} (last active: ${new Date(mem.last_updated).toLocaleString()})\n\n` +
          `Project: **${mem.project_folder_name || mem.project_id}**`
        );

        // Restore project state
        if (mem.project_folder_id) {
          dom.driveInput.value = mem.project_folder_id;
          dom.projectName.textContent = mem.project_folder_name || "SciKick";

          // Store project folder ID for tab matching
          projectFolderId = mem.project_folder_id;

          // Fetch the project file list for tab matching
          try {
            const filesRes = await fetch(`${SERVER_URL}/drive/folder/${mem.project_folder_id}/files`);
            if (filesRes.ok) {
              const filesData = await filesRes.json();
              projectFiles = filesData.files || [];
              detectCurrentTab(); // Refresh tab bar — may now match a project file
            }
          } catch (e) {
            // Non-critical — tab matching just won't work
          }
        }

        projectLoaded = true;
        applyInputState();

        // Project goal — recap if set, otherwise start onboarding. Replaces
        // the generic session-focus picker when a project is active.
        if (mem.goal && mem.goal.mode) {
          showGoalRecap(mem.goal);
        } else {
          showGoalOnboarding();
        }
      } else {
        // No active session — fresh start with no project loaded. Show the
        // generic brainstorming picker so the user can start chatting before
        // loading a project.
        if (!sessionFocus) showOnboardingOptions();
      }
    }
  } catch (e) {
    // No existing session
  }
}

// ---------------------------------------------------------------------------
// Project Loading
// ---------------------------------------------------------------------------

dom.driveInput.addEventListener("input", () => {
  dom.btnLoad.disabled = !dom.driveInput.value.trim() || !serverConnected;
});

dom.btnLoad.addEventListener("click", loadProject);

async function loadProject() {
  const raw = dom.driveInput.value.trim();
  if (!raw) return;

  // Extract folder ID from URL if needed
  let folderId = raw;
  const urlMatch = raw.match(/\/folders\/([a-zA-Z0-9_-]+)/);
  if (urlMatch) folderId = urlMatch[1];

  // Save for later sessions
  await chrome.storage.local.set({ driveFolderId: folderId });

  // Gate: if Drive isn't authenticated, point the user at the one-time setup
  // instead of letting resume fail with a confusing 401/404.
  if (!driveReady) {
    await checkDriveStatus(); // refresh in case auth just completed
  }
  if (!driveReady) {
    showSystemMessage(
      "📁 **Google Drive access needed**\n\n" +
      "SciKick needs its own Google Drive credentials to read this folder. " +
      "In a terminal, run:\n\n" +
      "`./start.sh --drive`\n\n" +
      "That runs a one-time Google Cloud setup (it explains each step). " +
      "Then come back and click **Load Project** again. " +
      `If you've already set up credentials, visit **${SERVER_URL}/drive/auth/url** ` +
      "to sign in with Google first."
    );
    return;
  }

  // Lock the chat + context controls and swap send → ✕ while the folder is
  // downloaded, so a message can't race the load and ✕ can cancel it.
  if (currentStream) currentStream.abort();
  currentStream = new AbortController();
  setUpdating(true);
  dom.btnLoad.textContent = "Loading...";
  loadingInProgress = true;

  try {
    // Use the resume endpoint — it loads files AND restores memory in one call
    showSystemMessage("🔄 Loading project from Google Drive...");

    const resumeRes = await fetch(`${SERVER_URL}/drive/folder/${folderId}/resume`, { signal: currentStream.signal });
    if (!resumeRes.ok) {
      const err = await resumeRes.json().catch(() => ({}));
      if (resumeRes.status === 401) {
        showSystemMessage(
          "🔐 Google authentication required.\n\n" +
          `Please visit **${SERVER_URL}/drive/auth/url** in your browser to sign in with Google, ` +
          "then click Load Project again."
        );
        return;
      }
      if (resumeRes.status === 404 || resumeRes.status === 403) {
        // Folder not accessible to the authenticated account (wrong ID, deleted,
        // or shared with a different account). Server sends a friendly detail.
        showSystemMessage(`⛔ ${err.detail || "Couldn't access that Google Drive folder."}`);
        return;
      }
      throw new Error(err.detail || "Failed to load project");
    }

    const data = await resumeRes.json();
    const { files, file_count, has_memory, resume_info, folder_name } = data;

    // Store project files and folder ID for tab matching
    projectFiles = files || [];
    projectFolderId = folderId;
    detectCurrentTab(); // Refresh tab bar — may now match a project file

    // Show file listing
    const sampleFiles = files.slice(0, 15); // show first 15
    let fileListStr = `📁 **${folder_name}** — ${file_count} files\n\n`;
    fileListStr += sampleFiles.map(f => `- ${f.name} (${formatSize(f.size)})`).join("\n");
    if (files.length > 15) {
      fileListStr += `\n- ... and ${files.length - 15} more files`;
    }
    showSystemMessage(fileListStr);

    // Handle resume
    if (has_memory && resume_info) {
      showSystemMessage(
        `📋 **Resumed session**\n\n` +
        `Last active: **${new Date(resume_info.last_updated).toLocaleString()}** on **${resume_info.last_computer}**\n\n` +
        `Previous context: ${resume_info.conversation_summary || "None"}`
      );
    } else {
      // Fresh project — initialise memory
      await fetch(`${SERVER_URL}/memory/init`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          folder_id: folderId,
          folder_name: folder_name,
        }),
        signal: currentStream.signal,
      });
    }

    // Examine the folder: list files, parse a clear manuscript if present,
    // and otherwise parse whatever supporting/comment docs are there. A missing
    // manuscript is a normal state (empty folder, or a non-manuscript mode),
    // not an error.
    showSystemMessage("📥 Downloading and examining your folder… (this might take a minute)");
    let projectLoadedFromServer = false;
    let loadData = null;
    try {
      const loadRes = await fetch(`${SERVER_URL}/drive/folder/${folderId}/load-context`, {
        method: "POST",
        signal: currentStream.signal,
      });
      if (loadRes.ok) {
        loadData = await loadRes.json();
        // Best-effort: surface the folder breakdown, project summary, and
        // manuscript status (if any). Guarded so an older server (missing
        // fields) can't break the load path.
        const fc = loadData.file_counts || {};
        const fcParts = [];
        if (fc.total != null) fcParts.push(`${fc.total} total`);
        if (fc.supplement) fcParts.push(`${fc.supplement} supplemental`);
        if (fc.supporting) fcParts.push(`${fc.supporting} supporting`);
        if (fc.reviewer_comments) {
          fcParts.push(`${fc.reviewer_comments} reviewer comment${fc.reviewer_comments > 1 ? "s" : ""}`);
        }
        if (fc.miscellaneous) fcParts.push(`${fc.miscellaneous} miscellaneous`);
        const fcLine = fcParts.length ? `📊 **Files in project**: ${fcParts.join(" · ")}\n\n` : "";
        const summaryLine = loadData.summary ? `📝 **Project summary**: ${loadData.summary}\n\n` : "";
        const ms = loadData.manuscript;
        const manuscriptLine = ms
          ? `📄 **Manuscript loaded**: ${ms.title || ms.name}\n` +
            `Sections: ${(ms.sections || []).join(", ")}\n` +
            `Figures found: ${(ms.figures || []).length}`
          : `📁 No single manuscript detected — I've examined the folder and parsed what's here. Paste paper text or scan a file when you want to go deeper.`;
        showSystemMessage(fcLine + summaryLine + manuscriptLine);
        // Scanned-page OCR hint: if the manuscript had pages the parser
        // couldn't recover, tell the user exactly what happened and how to fix
        // it — same actionable-command pattern as the Drive-setup gate. Only
        // fires when there's something to say. Wording depends on WHY the pages
        // were left unread, so we never wrongly claim "OCR not installed".
        const deficient = (ms && ms.ocr_deficient_pages) || [];
        const reason = (ms && ms.ocr_deficient_reason) || "";
        if (deficient.length > 0) {
          const n = deficient.length;
          const pg = `page${n > 1 ? "s" : ""}`;
          let msg;
          if (reason === "not_installed" || (reason === "" && pdfCaps && !pdfCaps.auto)) {
            // OCR deps missing — installing will recover these on reload.
            const cmd = (pdfCaps && pdfCaps.install_hint) ? pdfCaps.install_hint : "Run: ./start.sh --ocr";
            msg = `📄 ${n} scanned ${pg} in the manuscript couldn't be read (OCR not installed). ${cmd}, then reload the project to recover them.`;
          } else if (reason === "over_cap") {
            // OCR is installed, but the doc had more deficient pages than the
            // per-document cap — OCR was skipped entirely for this file.
            msg = `📄 ${n} scanned ${pg} in the manuscript couldn't be read (OCR is installed, but this file exceeds the per-document page cap, so OCR was skipped). Reload the project to retry, or ask about those pages individually.`;
          } else {
            // OCR ran but returned nothing for these pages (page_failed, or
            // unknown reason with OCR installed).
            msg = `📄 ${n} scanned ${pg} in the manuscript couldn't be read (OCR ran but couldn't recover them). Reload the project to retry, or paste those pages into the chat.`;
          }
          showSystemMessage(msg);
        }
        projectLoadedFromServer = true;
      } else {
        const err = await loadRes.json().catch(() => ({ detail: "Failed to load context" }));
        showSystemMessage(`⚠️ Couldn't examine that folder: ${err.detail}`);
      }
    } catch (e) {
      if (e.name === "AbortError") throw e; // cancelled — let the outer handler finish
      showSystemMessage(`⚠️ Context loading warning: ${e.message}`);
    }

    // User hit ✕ mid-load — skip the success steps below.
    if (currentStream.signal.aborted) throw new DOMException("aborted", "AbortError");

    // Best-effort context verification — used only to set the project-name
    // header. The folder breakdown, summary, and manuscript details are
    // already shown in the bubble above, so we don't repeat them here.
    try {
      const ctxRes = await fetch(`${SERVER_URL}/chat/context`);
      const ctxData = ctxRes.ok ? await ctxRes.json().catch(() => null) : null;
      if (ctxData && ctxData.loaded && ctxData.paper) {
        dom.projectName.textContent = ctxData.paper.title || folder_name;
      }
    } catch (e) {
      // Non-fatal — context verification only.
    }

    if (projectLoadedFromServer) {
      showSystemMessage(
        "✅ **Ready.** I've got the lay of the land — ask me about your project, or scan a document for deeper help."
      );
    }

    // Project goal — persisted per project in .scikick_memory.json. First load
    // (no goal yet) → walk the mode-specific onboarding. Subsequent loads →
    // recap what we know + the "say change goal" correction hint.
    try {
      const goal = loadData && loadData.goal;
      if (goal && goal.mode) {
        showGoalRecap(goal);
      } else {
        showGoalOnboarding();
      }
    } catch (e) {
      // Non-fatal — goal onboarding can be triggered with "change goal".
    }

    projectLoaded = true;
    applyInputState();

    // Show context window usage
    updateContextUsage();

    // Refresh info panel if open
    if (!dom.infoPanel.classList.contains("hidden")) loadInfoPanel();

  } catch (e) {
    if (e.name === "AbortError") {
      showSystemMessage("⏹️ Load cancelled.");
    } else {
      showSystemMessage(`❌ **Error loading project:** ${e.message}`);
    }
  } finally {
    currentStream = null;
    setUpdating(false); // restores send + input + context controls
    loadingInProgress = false;
    dom.btnLoad.textContent = "Load Project";
  }
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

dom.btnSend.addEventListener("click", sendMessage);
dom.btnStop.addEventListener("click", () => {
  // Interrupt the in-flight LLM stream; the abort surfaces in
  // sendMessage's catch as an AbortError (silently ignored).
  if (currentStream) currentStream.abort();
});
dom.chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
    return;
  }

  // Up/Down arrow: walk through submitted prompt history.
  // (Ignore when the user is holding a modifier or has a text selection,
  // so normal cursor movement still works.)
  if (e.altKey || e.ctrlKey || e.metaKey) return;
  if (dom.chatInput.selectionStart !== dom.chatInput.selectionEnd) return;

  if (e.key === "ArrowUp" && chatHistory.length) {
    e.preventDefault();
    if (historyIndex === -1) {
      // Entering history — stash the current draft to restore later.
      liveDraft = dom.chatInput.value;
      historyIndex = chatHistory.length - 1;
    } else if (historyIndex > 0) {
      historyIndex--;
    } else {
      return; // already at oldest entry
    }
    setChatInput(chatHistory[historyIndex]);
  } else if (e.key === "ArrowDown") {
    if (historyIndex === -1) return; // not navigating history
    e.preventDefault();
    if (historyIndex < chatHistory.length - 1) {
      historyIndex++;
      setChatInput(chatHistory[historyIndex]);
    } else {
      // Past the newest — restore the live draft.
      historyIndex = -1;
      setChatInput(liveDraft);
    }
  }
});

// Submitted prompts (oldest → newest) and the navigation pointer.
// historyIndex === -1 means we're at the live draft, not in history.
let chatHistory = [];
let historyIndex = -1;
let liveDraft = "";

// Replace the input contents (used when recalling history) and refit
// the textarea height, placing the caret at the end.
function setChatInput(text) {
  dom.chatInput.value = text;
  dom.chatInput.style.height = "auto";
  dom.chatInput.style.height = Math.min(dom.chatInput.scrollHeight, 180) + "px";
  const len = text.length;
  dom.chatInput.focus();
  dom.chatInput.setSelectionRange(len, len);
}

// Whether the chat input, send, and scan controls are interactive.
// Disabled when the server is down OR an LLM response is streaming —
// mirrors ChatGPT, where you can't queue another message mid-generation.
// Centralized so the 5s health poll can't re-enable input mid-stream.
function applyInputState() {
  const enabled = serverConnected && !generating && !contextUpdating;
  dom.chatInput.disabled = !enabled;
  dom.btnSend.disabled = !enabled;
  // Every context-mutating action locks whenever the chat is (server down,
  // streaming, or a context operation in flight) so two can't race.
  dom.btnScanTab.disabled = !enabled;
  dom.btnUseTab.disabled = !enabled;
  dom.btnLoad.disabled = !enabled;
}

// Show/hide the stop button to reflect whether a response is streaming,
// and lock the input + scan controls until it finishes (ChatGPT-style).
function setGenerating(isGenerating) {
  generating = isGenerating;
  dom.btnStop.classList.toggle("hidden", !isGenerating);
  dom.btnSend.classList.toggle("hidden", isGenerating); // swap send ↔ stop
  applyInputState();
}

// Mirror of setGenerating for any blocking context operation (Update,
// Scrape, Load project): while the operation's fetch is in flight, send
// swaps to ✕ (which aborts it) and the chat + context controls stay
// locked, so a message can't race the operation.
function setUpdating(isUpdating) {
  contextUpdating = isUpdating;
  dom.btnStop.classList.toggle("hidden", !isUpdating);
  dom.btnSend.classList.toggle("hidden", isUpdating); // swap send ↔ stop
  applyInputState();
}

async function sendMessage() {
  if (generating || contextUpdating) return; // streaming or a context refresh is in flight
  const text = dom.chatInput.value.trim();
  if (!text) return;

  // Record in prompt history (skip consecutive duplicates).
  if (chatHistory[chatHistory.length - 1] !== text) {
    chatHistory.push(text);
  }
  historyIndex = -1;
  liveDraft = "";

  // Clear input
  dom.chatInput.value = "";
  dom.chatInput.style.height = "auto";

  // Show user message
  addMessage("user", text);

  // Abort any existing stream
  if (currentStream) {
    currentStream.abort();
  }
  currentStream = new AbortController();
  setGenerating(true);

  let assistantBubble = null;
  let gotText = false;   // becomes true once any real content streams in
  let thinkingShown = false; // true once the "Thinking…" indicator replaces the typing dots

  try {
    assistantBubble = addMessage("assistant", "", true);
    // Show typing dots inside the empty assistant bubble
    assistantBubble.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
    let fullResponse = "";

    const res = await fetch(`${SERVER_URL}/chat/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        current_file: viewingFile || null,
        session_focus: sessionFocus,
      }),
      signal: currentStream.signal,
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      if (res.status === 400) {
        // Needs configuration (e.g. no API key configured) — show as guidance,
        // not a crash. Drop the empty typing-dots assistant bubble first.
        if (assistantBubble && assistantBubble.parentElement) {
          assistantBubble.parentElement.remove();
        }
        showSystemMessage(`⚙️ ${err.detail || "LLM not configured — open ⚙ Settings."}`);
        return;
      }
      throw new Error(err.detail || "Chat request failed");
    }

    // Stream the SSE response
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;

        let data;
        try {
          data = JSON.parse(line.slice(6));
        } catch (e) {
          // Partial JSON across a chunk boundary — wait for the rest of the line.
          continue;
        }

        if (data.type === "text") {
          // replace=true → reset the bubble to just this content (used by
          // the goal-finalize stream to swap the interim "Looking up…"
          // line for the final recap, instead of appending to it).
          if (data.replace) {
            fullResponse = data.content;
          } else {
            fullResponse += data.content;
          }
          gotText = true;
          renderAssistantMessage(assistantBubble, fullResponse);
        } else if (data.type === "thinking") {
          // Reasoning model (DeepSeek v4) is working through chain-of-thought
          // before any visible text — swap the typing dots for a progress
          // marker so the panel shows it's alive instead of dead air.
          if (!thinkingShown && assistantBubble) {
            thinkingShown = true;
            assistantBubble.innerHTML = '<div class="thinking-indicator">🧠 Thinking…</div>';
            scrollToBottom();
          }
        } else if (data.type === "warning") {
          // Stream succeeded but produced no text (e.g. the model exhausted
          // its output budget reasoning) — surface it instead of silence.
          fullResponse += `\n\n⚠️ ${data.content}`;
          gotText = true;
          renderAssistantMessage(assistantBubble, fullResponse);
        } else if (data.type === "error") {
          fullResponse += `\n\n⚠️ Error: ${data.content}`;
          gotText = true;
          renderAssistantMessage(assistantBubble, fullResponse);
        } else if (data.type === "goal_buttons") {
          // "change goal" intercept — re-show the mode picker after the
          // text message above has been rendered.
          showGoalOnboarding();
        }
      }
    }

    // Memory is now persisted server-side on stream completion (the /send
    // path is self-contained), so no follow-up /memory/update is needed here.

    // If the stream ended with no content and no warning/error was surfaced
    // (defensive — the server now sends a warning for empty completions),
    // drop the empty bubble and say so instead of leaving typing dots forever.
    if (!gotText && assistantBubble && assistantBubble.parentElement) {
      assistantBubble.parentElement.remove();
      addMessage("system", "⚠️ The assistant returned an empty response — the model likely spent its output budget reasoning. Try rephrasing or asking about a narrower part of the document.");
    }

    // Refresh context usage
    updateContextUsage();

    // Refresh info panel if open
    if (!dom.infoPanel.classList.contains("hidden")) loadInfoPanel();

  } catch (e) {
    if (e.name === "AbortError") {
      // User stopped generation. Drop the awaiting/thinking bubble if no
      // content ever arrived; otherwise keep the partial response.
      if (!gotText && assistantBubble && assistantBubble.parentElement) {
        assistantBubble.parentElement.remove();
      }
    } else {
      addMessage("system", `❌ ${e.message}`);
    }
  } finally {
    currentStream = null;
    setGenerating(false);
  }
}

// ---------------------------------------------------------------------------
// Message Rendering
// ---------------------------------------------------------------------------

function addMessage(role, content, returnBubble = false) {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role}`;

  const bubble = document.createElement("div");
  bubble.className = "message-content";

  if (role === "system") {
    // Simple markdown render for system messages
    bubble.innerHTML = renderMarkdown(content);
  } else if (role === "user") {
    bubble.textContent = content;
  }
  // For assistant, content is rendered via renderAssistantMessage

  wrapper.appendChild(bubble);
  dom.messages.appendChild(wrapper);
  scrollToBottom();

  if (returnBubble) return bubble;
  return null;
}

function renderAssistantMessage(bubble, content) {
  bubble.innerHTML = renderMarkdown(content);
  scrollToBottom();
}

function showSystemMessage(content) {
  addMessage("system", content);
}

function showOnboardingOptions() {
  const wrapper = document.createElement("div");
  wrapper.className = "message system";

  const bubble = document.createElement("div");
  bubble.className = "message-content";
  bubble.innerHTML = renderMarkdown("Welcome to SciKick, I'm your AI research companion. I'm here to bounce around ideas or help with your writing. So what would you like to work on today?");

  const btnRow = document.createElement("div");
  btnRow.className = "onboarding-buttons";

  const options = [
    { label: "🧠 Brainstorming / discussion", focus: "brainstorming" },
    { label: "📁 Project / writing", focus: null },
  ];

  options.forEach((opt) => {
    const btn = document.createElement("button");
    btn.className = "onboarding-btn";
    btn.textContent = opt.label;
    btn.addEventListener("click", () => {
      wrapper.remove();
      sessionFocus = opt.focus;
      if (opt.focus === "brainstorming") {
        addAssistantMessage(
          "Love it — I'm ready for whatever you've got! 🚀"
        );
      } else {
        addAssistantMessage(
          "Absolutely! 📁 Load the Google Drive folder associated with your work or document " +
          "and we'll dive in.\n\n⚠️ Heads-up: if you're using a **cloud-based LLM**, please don't " +
          "load any folder containing sensitive personal information — keep those out of the project."
        );
      }
      dom.chatInput.focus();
    });
    btnRow.appendChild(btn);
  });

  bubble.appendChild(btnRow);
  wrapper.appendChild(bubble);
  dom.messages.appendChild(wrapper);
  scrollToBottom();
}

// ---------------------------------------------------------------------------
// Project goal onboarding (per-project, persisted to .scikick_memory.json)
// ---------------------------------------------------------------------------

// The most recent goal-onboarding picker element, so a subtype picker (or a
// new "change goal") can replace it cleanly.
let _goalPickerEl = null;

function addAssistantMessage(text) {
  // Non-streamed assistant bubble — used for onboarding questions/recaps.
  const bubble = addMessage("assistant", "", true);
  renderAssistantMessage(bubble, text);
  return bubble;
}

function _goalButtonsRow(options, onClick) {
  const btnRow = document.createElement("div");
  btnRow.className = "onboarding-buttons";
  options.forEach((opt) => {
    const btn = document.createElement("button");
    btn.className = "onboarding-btn";
    btn.textContent = opt.label;
    btn.addEventListener("click", () => onClick(opt));
    btnRow.appendChild(btn);
  });
  return btnRow;
}

function showGoalOnboarding() {
  // Remove any existing picker so re-triggering ("change goal") doesn't stack.
  if (_goalPickerEl && _goalPickerEl.parentElement) _goalPickerEl.remove();

  const wrapper = document.createElement("div");
  wrapper.className = "message assistant";
  const bubble = document.createElement("div");
  bubble.className = "message-content";
  bubble.innerHTML = renderMarkdown(
    "**What's the goal for this project?**\n\nPick a mode and I'll tailor our work to it. " +
    "(You can change this anytime by saying **“change goal”**.)"
  );

  bubble.appendChild(
    _goalButtonsRow(
      [
        { label: "📝 Paper Revision", mode: "paper_revision" },
        { label: "✍️ Paper Writing", mode: "paper_writing" },
        { label: "💰 Grant", mode: "grant" },
        { label: "🎓 Application", mode: "application" },
        { label: "🧠 Brainstorming", mode: "brainstorming" },
        { label: "📄 Paper Discussion", mode: "paper_discussion" },
        { label: "💬 Other", mode: "other" },
      ],
      (opt) => pickGoalMode(opt.mode)
    )
  );

  wrapper.appendChild(bubble);
  dom.messages.appendChild(wrapper);
  _goalPickerEl = wrapper;
  scrollToBottom();
}

function pickGoalMode(mode) {
  if (mode === "grant") {
    showGoalSubtypes(mode, "What kind of grant are you aiming for?", [
      { label: "NIH", sub: "NIH" },
      { label: "NSF", sub: "NSF" },
      { label: "ERC", sub: "ERC" },
      { label: "Foundation", sub: "foundation" },
      { label: "Industry", sub: "industry" },
      { label: "Other", sub: "other" },
    ]);
    return;
  }
  if (mode === "application") {
    showGoalSubtypes(mode, "What kind of application is this?", [
      { label: "Job", sub: "job" },
      { label: "Med School", sub: "med_school" },
      { label: "Grad School / PhD", sub: "grad_school" },
      { label: "Other Professional", sub: "other_professional" },
    ]);
    return;
  }
  postGoal(mode, "");
}

function showGoalSubtypes(mode, prompt, subs) {
  if (_goalPickerEl && _goalPickerEl.parentElement) _goalPickerEl.remove();

  const wrapper = document.createElement("div");
  wrapper.className = "message assistant";
  const bubble = document.createElement("div");
  bubble.className = "message-content";
  bubble.innerHTML = renderMarkdown(`**${prompt}**`);
  bubble.appendChild(
    _goalButtonsRow(subs, (opt) => postGoal(mode, opt.sub))
  );
  wrapper.appendChild(bubble);
  dom.messages.appendChild(wrapper);
  _goalPickerEl = wrapper;
  scrollToBottom();
}

async function postGoal(mode, subtype) {
  if (_goalPickerEl && _goalPickerEl.parentElement) _goalPickerEl.remove();
  try {
    const res = await fetch(`${SERVER_URL}/chat/goal`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, subtype }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Failed to start goal onboarding");
    }
    const data = await res.json();
    if (data.done) {
      addAssistantMessage(data.recap || "Goal saved.");
    } else {
      // The AI "asks" the next free-text question — the user replies in chat,
      // and the server captures it (bypassing the LLM) on the next /chat/send.
      addAssistantMessage(data.question || "Tell me more.");
    }
  } catch (e) {
    showSystemMessage(`⚠️ ${e.message}`);
  }
}

function showGoalRecap(goal) {
  if (!goal || !goal.mode) return;
  addAssistantMessage(goal.recap || "Goal loaded.");
}

function renderMarkdown(text) {
  if (!text) return "";

  // Escape HTML-special chars FIRST so LLM output / scraped titles / folder
  // names can't inject live HTML (e.g. <img onerror=...>, <script>). The
  // markdown substitutions below only insert a fixed set of known-safe tags,
  // and the markdown sigils (*, `, #, -) are not HTML-special so they still
  // match after escaping.
  let html = escHtml(text)
    // Bold
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    // Italic
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    // Inline code
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    // Headers
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    // Unordered lists
    .replace(/^- (.+)$/gm, "<li>$1</li>")
    .replace(/((?:<li>.*<\/li>\n?)+)/g, "<ul>$1</ul>")
    // Line breaks
    .replace(/\n\n/g, "</p><p>")
    .replace(/\n/g, "<br>");

  // Wrap in paragraph if not already
  if (!html.startsWith("<")) {
    html = `<p>${html}</p>`;
  }

  // Clean up empty paragraphs
  html = html.replace(/<p>\s*<\/p>/g, "");

  return html;
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    dom.messages.parentElement.scrollTop = dom.messages.parentElement.scrollHeight;
  });
}

// ---------------------------------------------------------------------------
// Context window usage
// ---------------------------------------------------------------------------

async function updateContextUsage() {
  try {
    const res = await fetch(`${SERVER_URL}/chat/context-usage`);
    if (res.ok) {
      const data = await res.json();
      const pct = data.pct_used || 0;
      dom.ctxFill.style.width = `${Math.min(pct, 100)}%`;
      dom.ctxStats.textContent = `${data.pct_free}% free`;
      dom.ctxStats.title = `${data.total_used.toLocaleString()} / ${data.window_size.toLocaleString()} tokens used`;

      // Color coding
      dom.ctxBar.classList.remove("warning", "danger");
      if (pct > 90) {
        dom.ctxBar.classList.add("danger");
      } else if (pct > 70) {
        dom.ctxBar.classList.add("warning");
      }

      dom.ctxBar.classList.remove("hidden");
    }
  } catch (e) {
    // Ignore
  }
}

async function refreshContext() {
  dom.btnRefreshCtx.disabled = true;
  dom.btnRefreshCtx.textContent = "⏳";

  try {
    const res = await fetch(`${SERVER_URL}/chat/refresh-context`, {
      method: "POST",
    });
    if (res.ok) {
      const data = await res.json();
      // Backstop for older servers that returned {"status":"no_memory",...}
      // (HTTP 200, no `context`) when no session was active. Newer servers
      // always return a `context`; this just avoids crashing on old ones.
      if (data.status === "no_memory" || !data.context) {
        showSystemMessage(
          "⚠️ No active session on the server to refresh. Reload the project to reconnect."
        );
        return;
      }
      // Start a new conversation — clear the visible chat. The server already
      // dropped the chat history, kept docs, and scraped pages from context;
      // a digest of important points was saved to memory first (if any).
      dom.messages.innerHTML = "";

      const parts = [];
      if (data.dropped_docs_count) parts.push(`${data.dropped_docs_count} kept doc(s)`);
      if (data.dropped_scraped_count) parts.push(`${data.dropped_scraped_count} scraped article(s)`);
      if (data.chat_turns_cleared) parts.push(`${data.chat_turns_cleared} chat turn(s)`);

      if (data.project_loaded) {
        const droppedLine = parts.length
          ? `Dropped ${parts.join(", ")} from context.`
          : `Context cleared.`;
        showSystemMessage(
          `🔄 **Refreshed — new conversation.** Project context kept.\n\n` +
          droppedLine + (data.memory_flushed ? " Conversation digest saved to memory." : "") +
          `\nContext window: ${data.context.pct_free}% free (${data.context.remaining.toLocaleString()} tokens remaining).`
        );
      } else {
        showSystemMessage(
          `🔄 **Refreshed — ready for a new conversation.**\n\n` +
          (parts.length ? `Cleared ${parts.join(", ")}. ` : "") +
          `Context window: ${data.context.pct_free}% free (${data.context.remaining.toLocaleString()} tokens remaining).`
        );
      }

      updateContextUsage();
      // Refresh the info panel so the Loaded Documents / Scraped Articles
      // sections visibly empty after the drop.
      if (!dom.infoPanel.classList.contains("hidden")) loadInfoPanel();
      // The viewed file may no longer be kept — flip the scan button back to "Scan".
      refreshScanButtonState();
    } else {
      showSystemMessage("⚠️ Could not refresh context.");
    }
  } catch (e) {
    showSystemMessage(`⚠️ Refresh failed: ${e.message}`);
  } finally {
    dom.btnRefreshCtx.disabled = false;
    dom.btnRefreshCtx.textContent = "↺";
  }
}

// ---------------------------------------------------------------------------
// UI Helpers
// ---------------------------------------------------------------------------

dom.btnClear.addEventListener("click", () => {
  dom.messages.innerHTML = "";
  showSystemMessage("Chat cleared. I still remember your project context.");
});

dom.btnConnect.addEventListener("click", connect);

// Drive setup banner: re-check status after the user runs ./start.sh --drive
// or completes the browser OAuth consent.
dom.btnDriveRecheck.addEventListener("click", checkDriveStatus);
dom.btnDriveConnect.addEventListener("click", () => {
  // Opens the server's OAuth consent URL in a new tab. The user approves,
  // the server saves the token, then they click "Check again".
  chrome.tabs.create({ url: `${SERVER_URL}/drive/auth/url` });
});

// Restart session — wipe server state, chat, and re-show onboarding
dom.btnPower.addEventListener("click", async () => {
  dom.btnPower.classList.add("spinning");

  let memoryFlushed = false;
  if (serverConnected) {
    try {
      const res = await fetch(`${SERVER_URL}/chat/reset`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      memoryFlushed = !!data.memory_flushed;
    } catch (e) { /* ignore */ }
  }

  // Reset all client-side project state
  projectLoaded = false;
  projectFiles = [];
  projectFolderId = null;
  viewingFile = null;
  dom.projectName.textContent = "SciKick";

  // Wipe chat
  dom.messages.innerHTML = "";

  // If important points from the session were just digested + saved, let the
  // user know (only when something was actually saved — empty buffer restarts silently).
  if (memoryFlushed) {
    addMessage("system", "💾 Saved the important points from this session to memory. Starting fresh.");
  }

  // Reset session focus so onboarding options reappear
  sessionFocus = null;

  // Refresh the info panel if open
  if (!dom.infoPanel.classList.contains("hidden")) loadInfoPanel();

  // Refresh context usage (will show near-empty since memory is gone)
  if (serverConnected) updateContextUsage();

  // Show the "What would you like to work on today?" options
  showOnboardingOptions();

  setTimeout(() => dom.btnPower.classList.remove("spinning"), 600);
});

// Settings panel
dom.btnSettings.addEventListener("click", openSettings);
if (dom.btnSettingsClose) dom.btnSettingsClose.addEventListener("click", closeSettings);
dom.btnSettingsSave.addEventListener("click", saveSettings);
dom.cfgProvider.addEventListener("change", handleProviderChange);

// Info panel
dom.btnInfo.addEventListener("click", toggleInfoPanel);
if (dom.btnInfoClose) dom.btnInfoClose.addEventListener("click", closeInfoPanel);

// Search panel
dom.btnSearch.addEventListener("click", toggleSearchPanel);
if (dom.btnSearchClose) dom.btnSearchClose.addEventListener("click", closeSearchPanel);
dom.searchInput.addEventListener("input", runSearch);

// Pin panel
dom.btnPin.addEventListener("click", togglePinPanel);
if (dom.btnPinClose) dom.btnPinClose.addEventListener("click", closePinPanel);

// Right-click context menu (pin/unpin a message)
dom.messages.addEventListener("contextmenu", onMessageContextMenu);
dom.ctxMenuPin.addEventListener("click", onCtxPinClick);
document.addEventListener("click", (e) => {
  if (!dom.ctxMenu.contains(e.target)) hideContextMenu();
});
document.addEventListener("contextmenu", () => hideContextMenu());
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideContextMenu();
});

// Keep pinned-message staleness in sync whenever messages are added/removed
// (e.g. a cleared chat orphans the pinned DOM elements).
new MutationObserver(() => renderPinPanel()).observe(dom.messages, { childList: true });
// Thinking toggle is rebuilt inside #context-hint on every provider-hint
// render — delegate the click so it works without re-binding each time.
dom.contextHint.addEventListener("click", (e) => {
  const btn = e.target.closest(".tk-opt");
  if (btn) setThinkingMode(btn.dataset.mode);
});

async function toggleInfoPanel() {
  if (!dom.infoPanel.classList.contains("hidden")) {
    closeInfoPanel();
    return;
  }
  dom.infoPanel.classList.remove("hidden");
  await loadInfoPanel();
}

function closeInfoPanel() {
  dom.infoPanel.classList.add("hidden");
}

// --- Conversation search ---

function toggleSearchPanel() {
  if (!dom.searchPanel.classList.contains("hidden")) {
    closeSearchPanel();
    return;
  }
  dom.searchPanel.classList.remove("hidden");
  dom.searchInput.value = "";
  dom.searchResults.innerHTML = "";
  dom.searchInput.focus();
}

function closeSearchPanel() {
  dom.searchPanel.classList.add("hidden");
}

function runSearch() {
  const query = dom.searchInput.value.trim();
  dom.searchResults.innerHTML = "";

  if (!query) return;

  const q = query.toLowerCase();
  const messages = $$("#messages .message");
  const hits = [];

  messages.forEach((msg) => {
    const raw = msg.textContent || "";
    const lower = raw.toLowerCase();
    const rawIdx = lower.indexOf(q);
    if (rawIdx === -1) return;

    // Count all occurrences so the result badge can signal repeats.
    let count = 0;
    let pos = 0;
    while ((pos = lower.indexOf(q, pos)) !== -1) {
      count++;
      pos += q.length;
    }

    // Snippet centered on the first match, with the match highlighted.
    const start = Math.max(0, rawIdx - 40);
    const end = Math.min(raw.length, rawIdx + query.length + 60);
    const before = raw.slice(start, rawIdx).trim();
    const match = raw.slice(rawIdx, rawIdx + query.length);
    const after = raw.slice(rawIdx + query.length, end).trim();
    const snippet =
      (start > 0 ? "…" : "") +
      escHtml(before) +
      "<mark>" + escHtml(match) + "</mark>" +
      escHtml(after) +
      (end < raw.length ? "…" : "");

    const role = msg.classList.contains("user")
      ? "You"
      : msg.classList.contains("assistant")
        ? "Assistant"
        : "System";

    hits.push({ el: msg, role, snippet, count });
  });

  if (!hits.length) {
    dom.searchResults.innerHTML = '<div class="search-empty">No matches found.</div>';
    return;
  }

  dom.searchResults.innerHTML = hits.map((h, i) => `
    <button class="search-result" data-i="${i}">
      <span class="search-result-role">${h.role}</span>
      <span class="search-result-snippet">${h.snippet}</span>
      ${h.count > 1 ? `<span class="search-result-count">${h.count}×</span>` : ""}
    </button>
  `).join("");

  dom.searchResults.querySelectorAll(".search-result").forEach((btn) => {
    btn.addEventListener("click", () => {
      const hit = hits[Number(btn.dataset.i)];
      if (hit) jumpToMessage(hit.el);
    });
  });
}

function jumpToMessage(el) {
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.add("search-flash");
  setTimeout(() => el.classList.remove("search-flash"), 1500);
}

// --- Pinned messages ---

let pins = [];        // { el, role, text } — session-scoped (lost on panel reload)
let ctxTarget = null; // .message element last right-clicked

function togglePinPanel() {
  if (!dom.pinPanel.classList.contains("hidden")) {
    closePinPanel();
    return;
  }
  dom.pinPanel.classList.remove("hidden");
  renderPinPanel();
}

function closePinPanel() {
  dom.pinPanel.classList.add("hidden");
}

function messageRole(el) {
  if (el.classList.contains("user")) return "You";
  if (el.classList.contains("assistant")) return "Assistant";
  return "System";
}

function pinMessage(el) {
  if (pins.some((p) => p.el === el)) return;
  const text = (el.textContent || "").trim();
  if (!text) return;
  pins.push({ el, role: messageRole(el), text });
  renderPinPanel();
}

function unpinMessage(el) {
  pins = pins.filter((p) => p.el !== el);
  renderPinPanel();
}

function renderPinPanel() {
  if (dom.pinCount) {
    if (pins.length) {
      dom.pinCount.textContent = pins.length;
      dom.pinCount.classList.remove("hidden");
    } else {
      dom.pinCount.classList.add("hidden");
    }
  }
  dom.btnPin.title = pins.length ? `Pinned messages (${pins.length})` : "Pinned messages";

  if (!pins.length) {
    dom.pinList.innerHTML =
      '<div class="pin-empty">No pinned messages yet. Right-click a message and choose "Pin message".</div>';
    return;
  }

  dom.pinList.innerHTML = pins.map((p, i) => {
    const stale = !(p.el && p.el.isConnected);
    const preview = p.text.length > 160 ? p.text.slice(0, 160).trim() + "…" : p.text;
    return `
      <div class="pin-item${stale ? " pin-stale" : ""}" data-i="${i}">
        <button class="pin-jump" title="${stale ? "This message is no longer in the chat" : "Jump to message"}">
          <span class="pin-role">${p.role}</span>
          <span class="pin-text">${escHtml(preview)}</span>
        </button>
        <button class="pin-remove" title="Unpin">✕</button>
      </div>
    `;
  }).join("");

  dom.pinList.querySelectorAll(".pin-jump").forEach((btn) => {
    btn.addEventListener("click", () => jumpToPin(Number(btn.closest(".pin-item").dataset.i)));
  });
  dom.pinList.querySelectorAll(".pin-remove").forEach((btn) => {
    btn.addEventListener("click", () => {
      const i = Number(btn.closest(".pin-item").dataset.i);
      if (pins[i]) unpinMessage(pins[i].el);
    });
  });
}

function jumpToPin(i) {
  const p = pins[i];
  if (!p || !p.el || !p.el.isConnected) return;
  p.el.scrollIntoView({ behavior: "smooth", block: "center" });
  p.el.classList.add("search-flash");
  setTimeout(() => p.el.classList.remove("search-flash"), 1500);
}

// --- Right-click context menu ---

function onMessageContextMenu(e) {
  const msg = e.target.closest(".message");
  if (!msg) return;
  e.preventDefault();
  e.stopPropagation();
  ctxTarget = msg;
  dom.ctxMenuPin.textContent = pins.some((p) => p.el === msg) ? "📌 Unpin message" : "📌 Pin message";
  dom.ctxMenu.classList.remove("hidden");

  // Measure, then clamp within the viewport so the menu never clips.
  const rect = dom.ctxMenu.getBoundingClientRect();
  const left = Math.min(e.clientX, window.innerWidth - rect.width - 8);
  const top = Math.min(e.clientY, window.innerHeight - rect.height - 8);
  dom.ctxMenu.style.left = Math.max(8, left) + "px";
  dom.ctxMenu.style.top = Math.max(8, top) + "px";
}

function onCtxPinClick() {
  if (!ctxTarget) return;
  if (pins.some((p) => p.el === ctxTarget)) {
    unpinMessage(ctxTarget);
  } else {
    pinMessage(ctxTarget);
  }
  hideContextMenu();
}

function hideContextMenu() {
  dom.ctxMenu.classList.add("hidden");
  ctxTarget = null;
}

async function loadInfoPanel() {
  // Fetch data from all relevant endpoints in parallel
  const [ctxRes, memRes] = await Promise.all([
    fetch(`${SERVER_URL}/chat/context`).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch(`${SERVER_URL}/memory/status`).then(r => r.ok ? r.json() : null).catch(() => null),
  ]);

  if (!ctxRes && !memRes) {
    dom.infoBody.innerHTML = '<div class="info-empty">Could not fetch data. Is the server running?</div>';
    return;
  }

  let html = "";

  // --- Scraped Articles ---
  if (ctxRes && ctxRes.scraped_papers && ctxRes.scraped_papers.length > 0) {
    html += '<hr class="info-divider">';
    html += '<div class="info-section">';
    html += '<div class="info-section-title">🌐 Scraped Articles</div>';
    html += `<div class="info-row"><span class="info-label">Count</span><span class="info-value">${ctxRes.scraped_papers.length}</span></div>`;
    ctxRes.scraped_papers.forEach((sp, i) => {
      html += `<div class="info-row"><span class="info-label">#${i + 1}</span><span class="info-value">${escHtml(sp.title || "Untitled")}</span><button class="info-delete info-delete-scrape" data-index="${i}" title="Unload this page from context">✕</button></div>`;
      html += `<div class="info-row"><span class="info-label">Size</span><span class="info-value">${formatSize(sp.full_text_length || 0)}</span></div>`;
    });
    html += '</div>';
  }

  // --- Loaded Documents (files the user asked to keep in context) ---
  if (ctxRes && ctxRes.loaded_docs && ctxRes.loaded_docs.length > 0) {
    html += '<hr class="info-divider">';
    html += '<div class="info-section">';
    html += '<div class="info-section-title">📌 Loaded Documents</div>';
    html += `<div class="info-row"><span class="info-label">Count</span><span class="info-value">${ctxRes.loaded_docs.length}</span></div>`;
    ctxRes.loaded_docs.forEach((d, i) => {
      html += `<div class="info-row"><span class="info-label">#${i + 1}</span><span class="info-value">${escHtml(d.name || "Untitled")}</span><button class="info-delete info-unload-doc" data-index="${i}" title="Unload this file from context">✕</button></div>`;
      html += `<div class="info-row"><span class="info-label">Size</span><span class="info-value">${formatSize(d.chars || 0)}</span></div>`;
    });
    html += `<div class="info-row"><span class="info-label">Tip</span><span class="info-value">Click ✕ to unload a file from context.</span></div>`;
    html += '</div>';
  }

  // --- Project Data (file tree) ---
  if (projectFiles && projectFiles.length > 0) {
    const tree = buildFileTree(projectFiles);
    if (tree) {
      html += '<hr class="info-divider">';
      html += '<div class="info-section">';
      html += '<div class="info-section-title"><span>📁 Project Data</span><button class="info-delete info-unload-project" title="Unload project (keeps scraped articles)">✕ Unload</button></div>';
      html += `<div class="info-row"><span class="info-label">Total files</span><span class="info-value">${projectFiles.length}</span></div>`;
      html += `<div class="info-row"><span class="info-label">Papers</span><span class="info-value">${tree.paperCount}</span></div>`;
      html += `<div class="info-row"><span class="info-label">Sheets</span><span class="info-value">${tree.sheetCount}</span></div>`;
      html += `<div class="info-row"><span class="info-label">Tip</span><span class="info-value">Click a file to scan &amp; keep it in context</span></div>`;
      html += '<div class="tree-root">';
      html += renderFileTree(tree, 0);
      html += '</div>';
      html += '</div>';
    }
  }

  // --- Session / Memory ---
  if (memRes && memRes.active) {
    const m = memRes.memory;
    html += '<hr class="info-divider">';
    html += '<div class="info-section">';
    html += '<div class="info-section-title">💾 Session</div>';
    if (m.project_folder_name) {
      html += `<div class="info-row"><span class="info-label">Project</span><span class="info-value">${escHtml(m.project_folder_name)}</span></div>`;
    }
    if (m.last_updated) {
      html += `<div class="info-row"><span class="info-label">Last active</span><span class="info-value">${new Date(m.last_updated).toLocaleString()}</span></div>`;
    }
    if (m.last_computer) {
      html += `<div class="info-row"><span class="info-label">Computer</span><span class="info-value">${escHtml(m.last_computer)}</span></div>`;
    }
    if (m.chat_history) {
      html += `<div class="info-row"><span class="info-label">Chat turns</span><span class="info-value">${Math.floor(m.chat_history.length / 2)}</span></div>`;
    }
    if (m.decisions) {
      html += `<div class="info-row"><span class="info-label">Decisions</span><span class="info-value">${m.decisions.length}</span></div>`;
    }
    html += '</div>';
  }

  // --- Nothing loaded ---
  if (!html) {
    html = '<div class="info-empty">No data loaded. Load a project or scrape a paper to see details here.</div>';
  }

  dom.infoBody.innerHTML = html;
}

// Theme toggle
dom.btnTheme.addEventListener("click", toggleTheme);

async function toggleTheme() {
  const html = document.documentElement;
  const isLight = html.classList.toggle("light-theme");
  dom.btnTheme.textContent = isLight ? "☀️" : "🌙";
  dom.btnTheme.title = isLight ? "Switch to dark theme" : "Switch to light theme";
  await chrome.storage.local.set({ theme: isLight ? "light" : "dark" });
}

async function loadTheme() {
  const stored = await chrome.storage.local.get(["theme"]);
  if (stored.theme === "light") {
    document.documentElement.classList.add("light-theme");
    dom.btnTheme.textContent = "☀️";
    dom.btnTheme.title = "Switch to dark theme";
  }
}

// Context refresh
dom.btnRefreshCtx.addEventListener("click", refreshContext);

dom.chatInput.addEventListener("input", () => {
  // Auto-resize textarea
  dom.chatInput.style.height = "auto";
  dom.chatInput.style.height = Math.min(dom.chatInput.scrollHeight, 180) + "px";
});

// Manual drag-to-resize handle (top-left corner)
(function() {
  const handle = document.getElementById("resize-handle");
  let resizing = false;
  let startY = 0;
  let startHeight = 0;

  handle.addEventListener("mousedown", (e) => {
    resizing = true;
    startY = e.clientY;
    startHeight = dom.chatInput.offsetHeight;
    document.body.style.cursor = "ns-resize";
    document.body.style.userSelect = "none";
    e.preventDefault();
  });

  document.addEventListener("mousemove", (e) => {
    if (!resizing) return;
    const delta = startY - e.clientY; // drag up = taller
    const newHeight = Math.max(36, Math.min(180, startHeight + delta));
    dom.chatInput.style.height = newHeight + "px";
  });

  document.addEventListener("mouseup", () => {
    if (!resizing) return;
    resizing = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  });
})();

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatSize(bytes) {
  if (!bytes || bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

function escHtml(text) {
  if (!text) return "";
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// File Tree Helpers (for info panel)
// ---------------------------------------------------------------------------

function isFilePaper(file) {
  const paperMimes = [
    "application/pdf",
    "application/vnd.google-apps.document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ];
  if (paperMimes.includes(file.mimeType)) return true;
  const lower = file.name.toLowerCase();
  return lower.endsWith(".pdf") || lower.endsWith(".docx");
}

function isFileSheet(file) {
  const sheetMimes = [
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ];
  if (sheetMimes.includes(file.mimeType)) return true;
  const lower = file.name.toLowerCase();
  return lower.endsWith(".xlsx") || lower.endsWith(".xls") || lower.endsWith(".csv");
}

function buildFileTree(projectFiles) {
  if (!projectFiles || projectFiles.length === 0) return null;

  const root = {
    name: "root",
    path: "",
    files: [],
    subdirs: {},
    paperCount: 0,
    sheetCount: 0,
    dirCount: 0,
    isLeaf: false,
  };

  for (const file of projectFiles) {
    const parts = file.name.split("/");
    let current = root;
    for (let i = 0; i < parts.length; i++) {
      if (i === parts.length - 1) {
        current.files.push({
          name: parts[i],
          id: file.id,
          mimeType: file.mimeType,
          size: file.size,
          modifiedTime: file.modifiedTime,
          isLeaf: true,
          isPaper: isFilePaper(file),
          isSheet: isFileSheet(file),
        });
      } else {
        const dirName = parts[i];
        if (!current.subdirs[dirName]) {
          const dirPath = parts.slice(0, i + 1).join("/");
          current.subdirs[dirName] = {
            name: dirName,
            path: dirPath,
            files: [],
            subdirs: {},
            paperCount: 0,
            sheetCount: 0,
            dirCount: 0,
            isLeaf: false,
          };
        }
        current.dirCount = Object.keys(current.subdirs).length;
        current = current.subdirs[dirName];
      }
    }
  }

  computeFileCounts(root);
  return root;
}

function computeFileCounts(node) {
  if (node.isLeaf) return { papers: node.isPaper ? 1 : 0, sheets: node.isSheet ? 1 : 0 };

  let papers = 0, sheets = 0;
  for (const f of node.files) {
    if (f.isPaper) papers++;
    if (f.isSheet) sheets++;
  }
  node.dirCount = Object.keys(node.subdirs).length;
  for (const sub of Object.values(node.subdirs)) {
    const subCounts = computeFileCounts(sub);
    papers += subCounts.papers;
    sheets += subCounts.sheets;
  }
  node.paperCount = papers;
  node.sheetCount = sheets;
  return { papers, sheets };
}

function renderFileTree(node, depth) {
  if (node.isLeaf) {
    let icon = "📎";
    if (node.isPaper) icon = "📄";
    else if (node.isSheet) icon = "📊";

    return (
      `<div class="tree-row tree-file-row" style="padding-left:${depth * 14 + 18}px" ` +
      `data-file-name="${escHtml(node.name)}" title="Click to scan & keep this file in context">` +
      `<span class="tree-icon">${icon}</span>` +
      `<span class="tree-name">${escHtml(node.name)}</span>` +
      `<span class="tree-size">${formatSize(node.size)}</span>` +
      `<span class="tree-keep-hint">📌 keep</span>` +
      `</div>`
    );
  }

  const hasChildren = node.files.length > 0 || Object.keys(node.subdirs).length > 0;
  const expanded = depth === 0;

  const toggleHtml = hasChildren
    ? `<span class="tree-toggle${expanded ? " expanded" : ""}" data-path="${escHtml(node.path)}">▶</span>`
    : `<span class="tree-toggle empty">▶</span>`;

  const nameHtml = depth === 0
    ? `<span class="tree-name" style="font-weight:600">Project Root</span>`
    : `<span class="tree-name">${escHtml(node.name)}</span>`;

  let countHtml = "";
  if (hasChildren) {
    const parts = [];
    if (node.paperCount > 0) parts.push(`${node.paperCount}p`);
    if (node.sheetCount > 0) parts.push(`${node.sheetCount}s`);
    if (node.dirCount > 0) parts.push(`${node.dirCount}d`);
    if (parts.length > 0) {
      countHtml = `<span class="tree-count">${parts.join(" ")}</span>`;
    }
  }

  let html = "";
  html += `<div class="tree-node">`;
  html += `<div class="tree-row" style="padding-left:${depth * 14 + 4}px">`;
  html += toggleHtml;
  html += `<span class="tree-icon">📁</span>`;
  html += nameHtml;
  html += countHtml;
  html += `</div>`;

  html += `<div class="tree-children${expanded ? " expanded" : ""}" data-children="${escHtml(node.path)}">`;

  const dirNames = Object.keys(node.subdirs).sort((a, b) => a.localeCompare(b));
  for (const dirName of dirNames) {
    html += renderFileTree(node.subdirs[dirName], depth + 1);
  }
  node.files.sort((a, b) => a.name.localeCompare(b.name));
  for (const f of node.files) {
    html += `<div class="tree-node tree-file">`;
    html += renderFileTree(f, depth + 1);
    html += `</div>`;
  }

  html += `</div>`;
  html += `</div>`;
  return html;
}

// ---------------------------------------------------------------------------
// Current Tab Detection
// ---------------------------------------------------------------------------

/**
 * Parse Google Drive URLs to extract folder or file IDs.
 * Returns { type: "folder"|"file", id: string } or null.
 */
function parseDriveUrl(url) {
  if (!url) return null;

  // Drive folder: drive.google.com/drive/folders/<id>
  let m = url.match(/drive\.google\.com\/drive\/(?:u\/\d+\/)?folders\/([a-zA-Z0-9_-]+)/);
  if (m) return { type: "folder", id: m[1] };

  // Drive file: drive.google.com/file/d/<id>
  m = url.match(/drive\.google\.com\/file\/d\/([a-zA-Z0-9_-]+)/);
  if (m) return { type: "file", id: m[1] };

  // Google Docs: docs.google.com/document/d/<id>
  m = url.match(/docs\.google\.com\/document\/d\/([a-zA-Z0-9_-]+)/);
  if (m) return { type: "file", id: m[1] };

  // Google Sheets: docs.google.com/spreadsheets/d/<id>
  m = url.match(/docs\.google\.com\/spreadsheets\/d\/([a-zA-Z0-9_-]+)/);
  if (m) return { type: "file", id: m[1] };

  // Google Slides: docs.google.com/presentation/d/<id>
  m = url.match(/docs\.google\.com\/presentation\/d\/([a-zA-Z0-9_-]+)/);
  if (m) return { type: "file", id: m[1] };

  return null;
}

/**
 * Extract a readable domain label from a URL.
 */
function domainLabel(url) {
  if (!url) return "";
  try {
    const host = new URL(url).hostname;
    return host.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/**
 * Update the tab bar with the given tab info from the background worker.
 * (Side panels can't read url/title directly — the worker relays it.)
 */
function updateTabBar(tab) {
  if (!tab || !tab.url) {
    dom.tabBar.classList.add("hidden");
    dom.projectBar.classList.add("hidden");
    viewingFile = null;
    return;
  }

  // Skip chrome:// and extension pages
  if (tab.url.startsWith("chrome://") || tab.url.startsWith("chrome-extension://")) {
    dom.tabBar.classList.add("hidden");
    dom.projectBar.classList.add("hidden");
    viewingFile = null;
    return;
  }

  dom.tabBar.classList.remove("hidden");
  // Scan-this-file button is shown only when a project file is being viewed
  // (set in the Drive-file branch below). Hidden by default each update.
  dom.btnScanTab.classList.add("hidden");
  viewingFile = null;

  // Default: hide project bar — only show for Drive folders
  dom.projectBar.classList.add("hidden");
  // Default: not on a loadable Drive folder; checkDriveStatus() re-shows the
  // banner only when we actually land on one.
  currentDriveFolderId = null;
  dom.driveSetupBanner.style.display = "none";

  // Check for Google Drive URLs
  const driveInfo = parseDriveUrl(tab.url);
  if (driveInfo) {
    dom.tabBar.classList.add("drive-tab");

    if (driveInfo.type === "folder") {
      // Drive folder — show the project bar for loading
      dom.projectBar.classList.remove("hidden");
      dom.tabIcon.textContent = "📁";
      // Check if this is the project folder itself
      if (projectFolderId && driveInfo.id === projectFolderId) {
        dom.tabTitle.textContent = "Project folder";
        dom.tabBar.classList.add("viewing-project-file");
        dom.btnUseTab.classList.add("hidden"); // already loaded
        delete dom.btnUseTab.dataset.folderId;
        delete dom.btnUseTab.dataset.scrapeUrl;
      } else {
        dom.tabTitle.textContent = tab.title || "Untitled";
        dom.tabBar.classList.remove("viewing-project-file");
        delete dom.btnUseTab.dataset.scrapeUrl;
        dom.btnUseTab.dataset.folderId = driveInfo.id;
        currentDriveFolderId = driveInfo.id;
        // "Use this folder" only appears once Drive is ready; until then
        // checkDriveStatus() shows the setup banner with ./start.sh --drive
        // or a Connect button.
        if (driveReady) {
          dom.btnUseTab.classList.remove("hidden");
          dom.btnUseTab.textContent = "Use this folder";
        } else {
          dom.btnUseTab.classList.add("hidden");
        }
        checkDriveStatus();
      }
    } else {
      // It's a Drive file — check if it belongs to the loaded project
      dom.btnUseTab.classList.add("hidden");
      delete dom.btnUseTab.dataset.folderId;
      delete dom.btnUseTab.dataset.scrapeUrl;
      const match = projectFiles.find(f => f.id === driveInfo.id);
      if (match) {
        dom.tabIcon.textContent = "📄";
        dom.tabTitle.textContent = `Viewing: ${match.name}`;
        dom.tabBar.classList.add("viewing-project-file");
        viewingFile = { name: match.name, id: match.id };
        // Offer a one-click scan & keep of the file being viewed — or, if it's
        // already kept in context, an unload button routing to the same
        // mechanism as the ✕ in the Loaded Data panel.
        refreshScanButtonState();
      } else {
        dom.tabIcon.textContent = "📄";
        dom.tabTitle.textContent = tab.title || "Untitled";
        dom.tabBar.classList.remove("viewing-project-file");
      }
    }
  } else {
    // Non-Drive webpage — offer to scrape it as a paper, or unload it if
    // this page is already scraped (same mechanism as the ✕ in the Loaded
    // Data panel).
    dom.tabIcon.textContent = "🌐";
    dom.tabBar.classList.remove("drive-tab", "viewing-project-file");
    currentTabUrl = tab.url;
    delete dom.btnUseTab.dataset.folderId;
    dom.btnUseTab.dataset.scrapeUrl = tab.url;
    dom.tabTitle.textContent = tab.title || "Untitled";
    refreshScrapeButtonState();
  }

  dom.tabDomain.textContent = domainLabel(tab.url);
}

/**
 * Query the active tab via the background service worker.
 * The side panel cannot read url/title from chrome.tabs.query directly.
 */
async function detectCurrentTab(retries = 3) {
  for (let i = 0; i <= retries; i++) {
    try {
      const response = await chrome.runtime.sendMessage({ type: "getCurrentTab" });

      if (response && response.ok) {
        updateTabBar({ title: response.title, url: response.url, id: response.id });
        return;
      }
    } catch (err) {
      console.warn(`[TabBar] Attempt ${i + 1}/${retries + 1} failed:`, err.message);
    }

    if (i < retries) {
      // Wait before retrying — the service worker may still be waking up
      await new Promise(r => setTimeout(r, 500 * (i + 1)));
    }
  }

  // All retries exhausted
  console.error("[TabBar] All retries failed — service worker may be inactive");
  dom.tabBar.classList.add("hidden");
  viewingFile = null;
}

/**
 * Refresh the scan/update button for the file currently viewed in the tab.
 * If the file is already kept in the loaded-documents set, the button becomes
 * an "Update" action (re-fetch the latest content from Drive); otherwise it
 * stays "Scan".
 */
async function refreshScanButtonState() {
  if (!viewingFile || !viewingFile.id) {
    dom.btnScanTab.classList.add("hidden");
    return;
  }
  let kept = false;
  try {
    const res = await fetch(`${SERVER_URL}/chat/context`);
    if (res.ok) {
      const data = await res.json();
      kept = (data.loaded_docs || []).some(d => d.file_id === viewingFile.id)
          || data.manuscript_file_id === viewingFile.id;
    }
  } catch (err) { /* ignore — default to scan mode */ }
  if (kept) {
    dom.btnScanTab.textContent = "🔄 Update";
    dom.btnScanTab.title = "Update this file's context from Drive (cache-aware)";
    dom.btnScanTab.dataset.kept = "true";
  } else {
    dom.btnScanTab.textContent = "📌 Scan";
    dom.btnScanTab.title = "Scan & keep this file in context";
    dom.btnScanTab.dataset.kept = "false";
  }
  dom.btnScanTab.classList.remove("hidden");
}

/**
 * Ask the server to re-fetch kept document(s) from Drive and refresh their
 * context text. Cache-aware: only documents whose content actually changed are
 * replaced — unchanged docs keep the exact same prompt bytes, so the provider's
 * prefix cache keeps serving them at the cache-hit price.
 *
 * @param {string|null} fileId  Drive id of one kept doc, or null for all.
 * @param {string|null} label   Human label for single-doc updates (tab bar).
 */
async function updateContext(fileId, label) {
  // Guard against re-entrancy. The chat + button are locked below, but a
  // queued second click must not stack another request — hammering Update
  // with no visible feedback was crashing the panel.
  if (contextUpdating) return;

  const qs = fileId ? `?file_id=${encodeURIComponent(fileId)}` : "";
  const prefix = label ? `${label}: ` : "";

  // Lock the chat input + scan button and swap send → ✕ while the refresh
  // runs, so a message can't race the update and the refresh can be stopped
  // with the same interrupt that aborts generation. The ✕ handler aborts
  // currentStream, which this fetch listens to.
  if (currentStream) currentStream.abort();
  currentStream = new AbortController();
  setUpdating(true);

  const progressBubble = addMessage("system", "", true);
  progressBubble.innerHTML = '<div class="update-indicator">🔄 Updating context…</div>';
  scrollToBottom();

  let message = "";
  try {
    let res;
    try {
      res = await fetch(`${SERVER_URL}/chat/update-context${qs}`, {
        method: "POST",
        signal: currentStream.signal,
      });
    } catch (err) {
      // User hit ✕ — the server may still finish the refresh; just stop waiting.
      message = err.name === "AbortError"
        ? "⏹️ Update cancelled."
        : `❌ Couldn't update context: ${err.message}`;
      return;
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      message = `❌ Couldn't update context: ${err.detail || res.status}`;
      return;
    }
    const data = await res.json().catch(() => ({}));
    const updated = data.updated || [];
    const unchanged = data.unchanged || [];
    const failed = data.failed || [];
    const parts = [];
    if (updated.length) parts.push(`✅ ${prefix}refreshed ${updated.length} (${updated.map(u => u.name).join(", ")})`);
    if (unchanged.length) parts.push(`ℹ️ ${unchanged.length} unchanged — cache kept`);
    if (failed.length) parts.push(`⚠️ ${failed.length} failed (${failed.map(f => f.name).join(", ")})`);
    if (!parts.length) parts.push("No kept documents to update");
    message = parts.join(" · ");
  } finally {
    currentStream = null;
    setUpdating(false); // restores send + input + scan button state
    progressBubble.remove();
    addMessage("system", message);
    updateContextUsage();
    if (!dom.infoPanel.classList.contains("hidden")) loadInfoPanel();
    refreshScanButtonState();
  }
}

/**
 * Refresh the scrape/unload button for the page currently viewed in the tab.
 * If the page is already scraped into context, the button becomes an "Unload
 * page" action; otherwise it stays "Scrape this page". Routes to the same
 * /chat/scraped endpoint as the ✕ button in the Loaded Data panel.
 */
async function refreshScrapeButtonState() {
  const scrapeUrl = dom.btnUseTab.dataset.scrapeUrl;
  if (!scrapeUrl) return; // not in scrape mode
  let scraped = false;
  try {
    const res = await fetch(`${SERVER_URL}/chat/context`);
    if (res.ok) {
      const data = await res.json();
      scraped = (data.scraped_papers || []).some(p => p.url && p.url === scrapeUrl);
    }
  } catch (err) { /* ignore — default to scrape mode */ }
  if (scraped) {
    dom.btnUseTab.textContent = "📤 Unload page";
    dom.btnUseTab.title = "Unload this page from context";
    dom.btnUseTab.dataset.kept = "true";
  } else {
    dom.btnUseTab.textContent = "Scrape this page";
    dom.btnUseTab.title = "Scrape this page as a paper";
    dom.btnUseTab.dataset.kept = "false";
  }
  dom.btnUseTab.classList.remove("hidden");
}

/** Wire up the tab action button (load folder or scrape page) */
function initTabBar() {
  // One-click scan & keep of the file currently viewed in the browser tab —
  // or unload it if it's already kept in context (same mechanism as the ✕
  // button in the Loaded Data panel).
  dom.btnScanTab.addEventListener("click", async () => {
    if (!viewingFile || !viewingFile.id) return;
    if (dom.btnScanTab.dataset.kept === "true") {
      // Already in context — refresh its content from Drive (cache-aware).
      // updateContext restores the button label/state itself on every path.
      await updateContext(viewingFile.id, viewingFile.name);
      return;
    }
    dom.chatInput.value = `scan and keep ${viewingFile.name} in context`;
    await sendMessage();
    // Scanning may have just added the file to context — refresh the label.
    refreshScanButtonState();
  });

  dom.btnUseTab.addEventListener("click", async () => {
    // "Load this folder" mode
    const folderId = dom.btnUseTab.dataset.folderId;
    if (folderId) {
      dom.driveInput.value = folderId;
      dom.driveInput.dispatchEvent(new Event("input")); // trigger validation
      dom.driveInput.scrollIntoView({ behavior: "smooth" });
      return;
    }

    // "Scrape this page" / "Unload page" mode
    const scrapeUrl = dom.btnUseTab.dataset.scrapeUrl;
    if (scrapeUrl) {
      // Already scraped — unload it via the scraped-papers endpoint.
      if (dom.btnUseTab.dataset.kept === "true") {
        try {
          await fetch(`${SERVER_URL}/chat/scraped?url=${encodeURIComponent(scrapeUrl)}`, { method: "DELETE" });
        } catch (err) { /* ignore */ }
        updateContextUsage();
        if (!dom.infoPanel.classList.contains("hidden")) loadInfoPanel();
        refreshScrapeButtonState();
        return;
      }

      // Lock the chat + context controls and swap send → ✕ while scraping,
      // so the scrape can be interrupted like an LLM response. The ✕ handler
      // aborts currentStream, which the /chat/scrape fetch listens to.
      if (currentStream) currentStream.abort();
      currentStream = new AbortController();
      setUpdating(true);
      dom.btnUseTab.textContent = "Scraping...";
      loadingInProgress = true;

      try {
        showSystemMessage(`🔍 Scraping paper from ${new URL(scrapeUrl).hostname}...`);

        // Extract the page HTML from the active tab using the user's
        // authenticated browser session (avoids 403 on journal sites).
        // Host access for all sites is declared as a REQUIRED permission
        // in the manifest (<all_urls>), granted at install time — no
        // runtime permission prompt is needed here.
        const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
        if (!tab || !tab.id) throw new Error("No active tab found");

        // chrome.scripting requires the "scripting" permission (in
        // manifest.json) plus host access to the tab's URL.
        let pageHtml = "";
        try {
          const results = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: () => document.documentElement.outerHTML,
          });
          pageHtml = results[0]?.result || "";
        } catch (scriptErr) {
          throw new Error(
            `Cannot read page content. If you just changed the manifest, ` +
            `reload SciKick (chrome://extensions → ↻).\n\n` +
            `Details: ${scriptErr.message}`
          );
        }

        if (!pageHtml || pageHtml.length < 500) {
          throw new Error("Page content is empty or too short — the page may not have finished loading.");
        }
        // executeScript isn't abortable, so if ✕ was hit while it ran, bail
        // out here before sending the scrape.
        if (currentStream.signal.aborted) throw new DOMException("aborted", "AbortError");

        const res = await fetch(`${SERVER_URL}/chat/scrape`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: scrapeUrl, html: pageHtml }),
          signal: currentStream.signal,
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: res.statusText }));
          throw new Error(err.detail || "Scrape failed");
        }

        const data = await res.json();
        const paperCount = data.scraped_count || 1;
        showSystemMessage(
          `✅ **Paper scraped successfully** (${paperCount} paper${paperCount > 1 ? "s" : ""} in context)\n\n` +
          `📄 **Title**: ${data.title}\n` +
          `📝 **Sections**: ${data.sections.join(", ") || "Body only"}\n` +
          `📊 **Content**: ~${Math.round(data.full_text_length / 1000)}k characters, ${data.abstract_length} char abstract\n\n` +
          `The paper is now loaded in your chat context. You can ask me anything about it.`
        );

        projectLoaded = true;
        dom.projectName.textContent = data.title || "Scraped Paper";
        updateContextUsage();

        // Refresh info panel if open
        if (!dom.infoPanel.classList.contains("hidden")) loadInfoPanel();

      } catch (e) {
        if (e.name === "AbortError") {
          showSystemMessage("⏹️ Scrape cancelled.");
        } else {
          showSystemMessage(`❌ **Scrape failed:** ${e.message}\n\nTry loading the paper via Google Drive instead.`);
        }
      } finally {
        currentStream = null;
        setUpdating(false); // restores send + input + context controls
        loadingInProgress = false;
        // Scrape may have just added the page to context — refresh the label
        // (flips to "Unload page" on success, stays "Scrape this page" on error).
        refreshScrapeButtonState();
      }
    }
  });

  // Tab changes are pushed proactively by the background service worker
  // via the port — no need for chrome.tabs listeners here.
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Viewport height fix — CSS viewport units (vh/dvh) resolve to the main
// browser window in Chrome side panels, not the panel itself. Use JS to
// pin html/body to the actual panel height so the flex layout works.
// ---------------------------------------------------------------------------
function fixViewportHeight() {
  const h = window.innerHeight;
  document.documentElement.style.height = h + "px";
  document.body.style.height = h + "px";
}
fixViewportHeight();
window.addEventListener("resize", fixViewportHeight);

async function init() {
  // Wire up every copy button on the page (first-run banner has two command
  // rows; the Drive setup banner has one). Each button copies its sibling
  // <code class="cmd-text"> and flashes ✓ briefly.
  document.querySelectorAll(".btn-copy-cmd").forEach((btn) => {
    btn.addEventListener("click", () => {
      const code = btn.parentElement.querySelector(".cmd-text");
      if (!code) return;
      navigator.clipboard.writeText(code.textContent).then(() => {
        btn.textContent = "✓";
        setTimeout(() => { btn.textContent = "📋"; }, 2000);
      });
    });
  });

  // Load saved settings
  const stored = await chrome.storage.local.get(["driveFolderId", "theme", "everConnected"]);
  if (stored.driveFolderId) {
    dom.driveInput.value = stored.driveFolderId;
  }
  everConnected = !!stored.everConnected;

  // Apply saved theme (before first paint — but we're already in init)
  loadTheme();

  // Open a port to the background worker.
  // The worker pushes tab changes via this port (proactive updates).
  //
  // Chrome recycles the MV3 service worker after ~30s idle, which closes the
  // port. The keep-alive ping below holds it open while the panel is active,
  // but a recycle (or the panel being backgrounded) can still disconnect it.
  // When that happens we reconnect after a short backoff so proactive tab
  // updates resume — otherwise they'd stop forever until the panel reloaded.
  let bgReconnectTimer = null;
  function connectBgPort() {
    bgPort = chrome.runtime.connect({ name: "sidepanel" });
    bgPort.onMessage.addListener((msg) => {
      if (msg.type === "activeTabChanged" && msg.tab) {
        updateTabBar(msg.tab);
      }
    });
    bgPort.onDisconnect.addListener(() => {
      // Expected under MV3: the service worker recycles after ~30s idle and
      // closes the port. debug (not warn) so this self-healing event doesn't
      // light up the chrome://extensions error button. Reconnect is automatic.
      console.debug("[TabBar] Background port disconnected — will reconnect");
      bgPort = null;
      // Service worker was recycled (or panel slept). Reconnect shortly; the
      // worker is recreated on-demand by chrome.runtime.connect.
      if (bgReconnectTimer) clearTimeout(bgReconnectTimer);
      bgReconnectTimer = setTimeout(connectBgPort, 2000);
    });
  }
  connectBgPort();

  // Ping every 20s to keep the service worker from going inactive. Also
  // serves as a reconnection safety net: if the port is null (a disconnect
  // happened and the scheduled reconnect hasn't fired/landed yet), reconnect
  // here so updates resume within one ping cycle.
  setInterval(() => {
    if (!bgPort) {
      try { connectBgPort(); } catch (e) { /* will retry next tick */ }
      return;
    }
    try { bgPort.postMessage({ type: "ping" }); } catch (e) {
      // Port died between ticks — force a reconnect.
      bgPort = null;
    }
  }, 20000);

  // Detect current tab and listen for changes
  initTabBar();
  detectCurrentTab();

  // Connect to server
  await connect();

  // Delegated click handler for info panel: tree toggles, delete, unload
  if (dom.infoBody) {
    dom.infoBody.addEventListener("click", async (e) => {
      // --- Tree toggle ---
      const toggle = e.target.closest(".tree-toggle");
      if (toggle && !toggle.classList.contains("empty")) {
        const path = toggle.dataset.path;
        if (path != null) {
          const children = dom.infoBody.querySelector(`[data-children="${CSS.escape(path)}"]`);
          if (children) {
            if (children.classList.contains("expanded")) {
              children.classList.remove("expanded");
              toggle.classList.remove("expanded");
            } else {
              children.classList.add("expanded");
              toggle.classList.add("expanded");
            }
          }
        }
      }

      // --- Scan & keep a project file (click its row in the tree) ---
      // Sends "scan and keep <filename> in context" so the server matches it
      // exactly (literal-substring) and adds it to the loaded-documents set —
      // no reliance on tab detection or phrasing.
      const fileRow = e.target.closest(".tree-file-row");
      if (fileRow) {
        const name = fileRow.dataset.fileName;
        if (name) {
          if (generating) return; // don't scan while a response is streaming
          dom.chatInput.value = `scan and keep ${name} in context`;
          closeInfoPanel();
          sendMessage();
        }
        return;
      }

      // --- Delete scraped article ---
      const delBtn = e.target.closest(".info-delete-scrape");
      if (delBtn) {
        const idx = delBtn.dataset.index;
        if (idx != null) {
          try {
            await fetch(`${SERVER_URL}/chat/scraped?index=${idx}`, { method: "DELETE" });
          } catch (err) { /* ignore */ }
          loadInfoPanel();
          // Keep the tab-bar scrape/unload label in sync with the change.
          refreshScrapeButtonState();
        }
      }

      // --- Unload a kept document from context ---
      const unloadDocBtn = e.target.closest(".info-unload-doc");
      if (unloadDocBtn) {
        const idx = unloadDocBtn.dataset.index;
        if (idx != null) {
          try {
            await fetch(`${SERVER_URL}/chat/loaded-docs?index=${idx}`, { method: "DELETE" });
          } catch (err) { /* ignore */ }
          loadInfoPanel();
          // Keep the tab-bar scan/update label in sync with the change.
          refreshScanButtonState();
        }
      }

      // --- Unload project ---
      if (e.target.closest(".info-unload-project")) {
        if (serverConnected) {
          try {
            await fetch(`${SERVER_URL}/chat/unload-project`, { method: "POST" });
          } catch (err) { /* ignore */ }
        }
        projectLoaded = false;
        projectFiles = [];
        projectFolderId = null;
        viewingFile = null;
        dom.projectName.textContent = "SciKick";
        loadInfoPanel();
      }
    });
  }

  // Poll health
  setInterval(checkServerHealth, POLL_INTERVAL_MS);
}

init();
