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

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let serverConnected = false;
let projectLoaded = false;
let projectFiles = []; // {id, name, mimeType} — files in the loaded project
let projectFolderId = null; // Drive folder ID of the loaded project
let viewingFile = null; // {name, id} — project file currently open in a browser tab
let currentTabUrl = null; // URL of the currently active browser tab
let sessionFocus = null; // "brainstorming" | "paper_discussion" | "paper_writing" | "revision" | "other"
let currentStream = null; // AbortController for SSE
let loadingInProgress = false; // true during loadProject / scrape — suppress disconnect banner
let healthFailCount = 0; // consecutive health check failures (prevents false disconnect flash)
let bgPort = null; // Port to background service worker (keep-alive only)

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
  btnTheme: $("#btn-theme"),
  btnClear: $("#btn-clear"),
  btnPower: $("#btn-power"),
  btnConnect: $("#btn-connect"),
  btnCopyCmd: $("#btn-copy-cmd"),
  cmdText: $("#cmd-text"),
  // Settings
  btnSettings: $("#btn-settings"),
  settingsPanel: $("#settings-panel"),
  btnSettingsClose: $("#btn-settings-close"),
  btnSettingsSave: $("#btn-settings-save"),
  cfgProvider: $("#cfg-provider"),
  cfgApiKey: $("#cfg-api-key"),
  cfgModel: $("#cfg-model"),
  cfgBaseUrl: $("#cfg-base-url"),
  cfgBaseUrlLabel: $("#cfg-base-url-label"),
  cfgStatus: $("#cfg-status"),
  // Info panel
  btnInfo: $("#btn-info"),
  infoPanel: $("#info-panel"),
  btnInfoClose: $("#btn-info-close"),
  infoBody: $("#info-body"),
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
    // Enable chat regardless of whether a project is loaded
    dom.chatInput.disabled = false;
    dom.btnSend.disabled = false;

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
    }
  } else {
    dom.connectBanner.style.display = "block";
    dom.driveInput.disabled = true;
    dom.btnLoad.disabled = true;
    dom.chatInput.disabled = true;
    dom.btnSend.disabled = true;

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
    // Show onboarding options immediately so the user can pick an interaction type
    // before loading a project
    if (!sessionFocus) showOnboardingOptions();
    // Check for existing session
    await checkExistingSession();
  }
}

async function showProviderInfo() {
  try {
    const res = await fetch(`${SERVER_URL}/chat/providers`);
    if (res.ok) {
      const data = await res.json();
      if (data.current && data.current.configured) {
        dom.contextHint.innerHTML = `🧠 <strong>${escHtml(data.current.provider)}</strong> / ${escHtml(data.current.model)}`;
        dom.contextHint.classList.remove("hidden");
      }
    }
  } catch (e) {
    // Provider info not critical
  }
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
      if (data.current) {
        dom.cfgProvider.value = data.current.provider || "anthropic";
        dom.cfgModel.value = data.current.model || "";
      }
    }
  } catch (e) {
    // Use defaults
  }
  dom.cfgApiKey.value = ""; // never pre-fill the key
  dom.settingsPanel.classList.remove("hidden");
  handleProviderChange();
}

function closeSettings() {
  dom.settingsPanel.classList.add("hidden");
  dom.cfgStatus.textContent = "";
  dom.cfgStatus.className = "";
}

function handleProviderChange() {
  const provider = dom.cfgProvider.value;
  if (provider === "custom") {
    dom.cfgBaseUrl.classList.remove("hidden");
    dom.cfgBaseUrlLabel.classList.remove("hidden");
  } else {
    dom.cfgBaseUrl.classList.add("hidden");
    dom.cfgBaseUrlLabel.classList.add("hidden");
  }
}

async function saveSettings() {
  const provider = dom.cfgProvider.value;
  const apiKey = dom.cfgApiKey.value.trim();
  const model = dom.cfgModel.value.trim();
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
      // Update the context hint
      if (data.current) {
        dom.contextHint.innerHTML = `🧠 <strong>${escHtml(data.current.provider)}</strong> / ${escHtml(data.current.model)}`;
        dom.contextHint.classList.remove("hidden");
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
        dom.chatInput.disabled = false;
        dom.btnSend.disabled = false;

        // Show onboarding options if no focus set yet
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

  dom.btnLoad.disabled = true;
  dom.btnLoad.textContent = "Loading...";
  loadingInProgress = true;

  try {
    // Use the resume endpoint — it loads files AND restores memory in one call
    showSystemMessage("🔄 Loading project from Google Drive...");

    const resumeRes = await fetch(`${SERVER_URL}/drive/folder/${folderId}/resume`);
    if (!resumeRes.ok) {
      const err = await resumeRes.json();
      if (resumeRes.status === 401) {
        showSystemMessage(
          "🔐 Google authentication required.\n\n" +
          `Please visit **${SERVER_URL}/drive/auth/url** in your browser to sign in with Google, ` +
          "then click Load Project again."
        );
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
      });
    }

    // Download and process the manuscript + reviewer comments from Drive
    showSystemMessage("📥 Downloading and processing manuscript...");
    try {
      const loadRes = await fetch(`${SERVER_URL}/drive/folder/${folderId}/load-context`, {
        method: "POST",
      });
      if (loadRes.ok) {
        const loadData = await loadRes.json();
        showSystemMessage(
          `📄 **Manuscript loaded**: ${loadData.manuscript.title || loadData.manuscript.name}\n` +
          `Sections: ${loadData.manuscript.sections.join(", ")}\n` +
          `Figures found: ${loadData.manuscript.figures.length}\n` +
          `Files in project: ${loadData.comments.count || 0}`
        );
      } else {
        const err = await loadRes.json().catch(() => ({ detail: "Failed to load context" }));
        showSystemMessage(`⚠️ Could not auto-detect manuscript: ${err.detail}\n\nYou can still chat, but you'll need to paste your paper text directly.`);
      }
    } catch (e) {
      showSystemMessage(`⚠️ Context loading warning: ${e.message}`);
    }

    // Verify what was loaded into the chat context (informational — guard
    // every field so an unexpected payload can't abort loadProject's success
    // path with a misleading error).
    try {
      const ctxRes = await fetch(`${SERVER_URL}/chat/context`);
      const ctxData = ctxRes.ok ? await ctxRes.json().catch(() => null) : null;

      if (ctxData && ctxData.loaded && ctxData.paper) {
        const paper = ctxData.paper;
        const sections = Array.isArray(paper.sections) ? paper.sections : [];
        const figures = Array.isArray(paper.figures) ? paper.figures : [];
        showSystemMessage(
          `✅ **Ready**\n\n` +
          `Paper: ${paper.title || "Untitled"}\n` +
          `Sections: ${sections.join(", ")}\n` +
          `Figures: ${figures.length}\n\n` +
          `You can now ask me anything about your project.`
        );
        dom.projectName.textContent = paper.title || folder_name;
      }
    } catch (e) {
      // Non-fatal — context verification only.
    }

    projectLoaded = true;
    dom.chatInput.disabled = false;
    dom.btnSend.disabled = false;

    // Show context window usage
    updateContextUsage();

    // Show onboarding focus options
    if (!sessionFocus) showOnboardingOptions();

    // Refresh info panel if open
    if (!dom.infoPanel.classList.contains("hidden")) loadInfoPanel();

  } catch (e) {
    showSystemMessage(`❌ **Error loading project:** ${e.message}`);
  } finally {
    loadingInProgress = false;
    dom.btnLoad.disabled = false;
    dom.btnLoad.textContent = "Load Project";
  }
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

dom.btnSend.addEventListener("click", sendMessage);
dom.chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

async function sendMessage() {
  const text = dom.chatInput.value.trim();
  if (!text) return;

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

  try {
    const assistantBubble = addMessage("assistant", "", true);
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
        if (line.startsWith("data: ")) {
          try {
            const data = JSON.parse(line.slice(6));
            if (data.type === "text") {
              fullResponse += data.content;
              renderAssistantMessage(assistantBubble, fullResponse);
            } else if (data.type === "error") {
              fullResponse += `\n\n⚠️ Error: ${data.content}`;
              renderAssistantMessage(assistantBubble, fullResponse);
            }
          } catch (e) {
            // partial JSON, ignore
          }
        }
      }
    }

    // Memory is now persisted server-side on stream completion (the /send
    // path is self-contained), so no follow-up /memory/update is needed here.

    // Refresh context usage
    updateContextUsage();

    // Refresh info panel if open
    if (!dom.infoPanel.classList.contains("hidden")) loadInfoPanel();

  } catch (e) {
    if (e.name !== "AbortError") {
      addMessage("system", `❌ ${e.message}`);
    }
  } finally {
    currentStream = null;
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
  bubble.innerHTML = renderMarkdown(
    "**What would you like to work on today?**\n\nChoose a focus area to help me tailor our conversation:"
  );

  const btnRow = document.createElement("div");
  btnRow.className = "onboarding-buttons";

  const options = [
    {
      label: "🧠 Brainstorming",
      focus: "brainstorming",
      prompt: "I'd like to brainstorm today. Help me explore ideas, develop hypotheses, and think through research directions.",
    },
    {
      label: "📄 Paper Discussion",
      focus: "paper_discussion",
      prompt: "I'd like to discuss my paper today. Help me think through the results, implications, and narrative of my manuscript.",
    },
    {
      label: "✍️ Paper Writing",
      focus: "paper_writing",
      prompt: "I'd like to work on writing today. Help me draft, edit, and refine sections of my manuscript.",
    },
    {
      label: "📝 Paper Revision",
      focus: "revision",
      prompt: "I'd like to work on peer review revisions today. Help me address reviewer comments and draft responses.",
    },
    {
      label: "💬 Other",
      focus: "other",
      prompt: "I have something else in mind today. Let me explain what I need help with.",
    },
  ];

  options.forEach((opt) => {
    const btn = document.createElement("button");
    btn.className = "onboarding-btn";
    btn.textContent = opt.label;
    btn.addEventListener("click", () => {
      wrapper.remove();
      sessionFocus = opt.focus;
      dom.chatInput.value = opt.prompt;
      dom.chatInput.dispatchEvent(new Event("input"));
      dom.chatInput.focus();
    });
    btnRow.appendChild(btn);
  });

  bubble.appendChild(btnRow);
  wrapper.appendChild(bubble);
  dom.messages.appendChild(wrapper);
  scrollToBottom();
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
  if (!projectLoaded) {
    showSystemMessage("💡 Load a project first to enable context management. You can still chat without one!");
    return;
  }
  dom.btnRefreshCtx.disabled = true;
  dom.btnRefreshCtx.textContent = "⏳";

  try {
    const res = await fetch(`${SERVER_URL}/chat/refresh-context`, {
      method: "POST",
    });
    if (res.ok) {
      const data = await res.json();
      showSystemMessage(
        `🔄 **Context refreshed**\n\n` +
        `Saved summary of ${data.turns_cleared} chat turns and ${data.decisions_saved} decisions to memory.\n` +
        `Context window: ${data.context.pct_free}% free (${data.context.remaining.toLocaleString()} tokens remaining).`
      );
      updateContextUsage();
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
      html += `<div class="info-row"><span class="info-label">#${i + 1}</span><span class="info-value">${escHtml(sp.title || "Untitled")}</span><button class="info-delete info-delete-scrape" data-index="${i}" title="Remove this article">✕</button></div>`;
      html += `<div class="info-row"><span class="info-label">Size</span><span class="info-value">${formatSize(sp.full_text_length || 0)}</span></div>`;
    });
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
      `<div class="tree-row" style="padding-left:${depth * 14 + 18}px">` +
      `<span class="tree-icon">${icon}</span>` +
      `<span class="tree-name" title="${escHtml(node.name)}">${escHtml(node.name)}</span>` +
      `<span class="tree-size">${formatSize(node.size)}</span>` +
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
  viewingFile = null;

  // Default: hide project bar — only show for Drive folders
  dom.projectBar.classList.add("hidden");

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
        dom.btnUseTab.classList.remove("hidden");
        dom.btnUseTab.textContent = "Use this folder";
        delete dom.btnUseTab.dataset.scrapeUrl;
        dom.btnUseTab.dataset.folderId = driveInfo.id;
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
      } else {
        dom.tabIcon.textContent = "📄";
        dom.tabTitle.textContent = tab.title || "Untitled";
        dom.tabBar.classList.remove("viewing-project-file");
      }
    }
  } else {
    // Non-Drive webpage — offer to scrape it as a paper
    dom.tabIcon.textContent = "🌐";
    dom.tabBar.classList.remove("drive-tab", "viewing-project-file");
    currentTabUrl = tab.url;
    dom.btnUseTab.classList.remove("hidden");
    dom.btnUseTab.textContent = "Scrape this page";
    delete dom.btnUseTab.dataset.folderId;
    dom.btnUseTab.dataset.scrapeUrl = tab.url;
    dom.tabTitle.textContent = tab.title || "Untitled";
  }

  dom.tabDomain.textContent = domainLabel(tab.url);
  console.log("[TabBar] Current tab:", tab.title, "|", domainLabel(tab.url), "| viewingFile:", viewingFile);
}

/**
 * Query the active tab via the background service worker.
 * The side panel cannot read url/title from chrome.tabs.query directly.
 */
async function detectCurrentTab(retries = 3) {
  for (let i = 0; i <= retries; i++) {
    try {
      const response = await chrome.runtime.sendMessage({ type: "getCurrentTab" });
      console.log("[TabBar] BG response:", response);

      if (response && response.ok) {
        updateTabBar({ title: response.title, url: response.url, id: response.id });
        return;
      } else {
        console.log("[TabBar] No tab from BG:", response);
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

/** Wire up the tab action button (load folder or scrape page) */
function initTabBar() {
  console.log("[TabBar] Initializing tab bar...");

  dom.btnUseTab.addEventListener("click", async () => {
    // "Load this folder" mode
    const folderId = dom.btnUseTab.dataset.folderId;
    if (folderId) {
      dom.driveInput.value = folderId;
      dom.driveInput.dispatchEvent(new Event("input")); // trigger validation
      dom.driveInput.scrollIntoView({ behavior: "smooth" });
      return;
    }

    // "Scrape this page" mode
    const scrapeUrl = dom.btnUseTab.dataset.scrapeUrl;
    if (scrapeUrl) {
      dom.btnUseTab.disabled = true;
      dom.btnUseTab.textContent = "Scraping...";
      loadingInProgress = true;
      showSystemMessage(`🔍 Scraping paper from ${new URL(scrapeUrl).hostname}...`);

      try {
        // Extract the page HTML from the active tab using the user's
        // authenticated browser session (avoids 403 on journal sites).
        const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
        if (!tab || !tab.id) throw new Error("No active tab found");

        // chrome.scripting requires the "scripting" permission (added in
        // manifest.json). Reload the extension if this fails.
        let pageHtml = "";
        try {
          const results = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: () => document.documentElement.outerHTML,
          });
          pageHtml = results[0]?.result || "";
        } catch (scriptErr) {
          throw new Error(
            `Cannot read page content. Make sure the extension was reloaded ` +
            `after the latest update (chrome://extensions → ↻).\n\n` +
            `Details: ${scriptErr.message}`
          );
        }

        if (!pageHtml || pageHtml.length < 500) {
          throw new Error("Page content is empty or too short — the page may not have finished loading.");
        }

        const res = await fetch(`${SERVER_URL}/chat/scrape`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: scrapeUrl, html: pageHtml }),
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
        showSystemMessage(`❌ **Scrape failed:** ${e.message}\n\nTry loading the paper via Google Drive instead.`);
      } finally {
        loadingInProgress = false;
        dom.btnUseTab.disabled = false;
        dom.btnUseTab.textContent = "Scrape this page";
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
  // Show the command to start the server.
  // chrome.runtime.getURL() returns an internal chrome-extension:// URL,
  // not a filesystem path, so we can't derive the real project directory.
  // The user should run this from the project root.
  const startCmd = `cd SciKick && ./start.sh`;
  dom.cmdText.textContent = startCmd;

  // Wire up the copy button
  dom.btnCopyCmd.addEventListener("click", () => {
    navigator.clipboard.writeText(startCmd).then(() => {
      dom.btnCopyCmd.textContent = "✓";
      setTimeout(() => { dom.btnCopyCmd.textContent = "📋"; }, 2000);
    });
  });

  // Load saved settings
  const stored = await chrome.storage.local.get(["driveFolderId", "theme"]);
  if (stored.driveFolderId) {
    dom.driveInput.value = stored.driveFolderId;
  }

  // Apply saved theme (before first paint — but we're already in init)
  loadTheme();

  // Open a port to the background worker.
  // The worker pushes tab changes via this port (proactive updates).
  bgPort = chrome.runtime.connect({ name: "sidepanel" });
  bgPort.onMessage.addListener((msg) => {
    if (msg.type === "activeTabChanged" && msg.tab) {
      updateTabBar(msg.tab);
    }
  });
  bgPort.onDisconnect.addListener(() => {
    console.warn("[TabBar] Background port disconnected");
    bgPort = null;
  });

  // Ping every 20s to keep the service worker from going inactive
  setInterval(() => {
    if (bgPort) {
      try { bgPort.postMessage({ type: "ping" }); } catch (e) { /* port closed */ }
    }
  }, 20000);

  console.log("[TabBar] Connected to background worker");

  // Detect current tab and listen for changes
  console.log("[TabBar] Starting tab detection...");
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

      // --- Delete scraped article ---
      const delBtn = e.target.closest(".info-delete-scrape");
      if (delBtn) {
        const idx = delBtn.dataset.index;
        if (idx != null) {
          try {
            await fetch(`${SERVER_URL}/chat/scraped?index=${idx}`, { method: "DELETE" });
          } catch (err) { /* ignore */ }
          loadInfoPanel();
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
