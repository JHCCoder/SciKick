# <img src="SciKick_logo.svg" width="16" height="28" alt="logo"> SciKick <img src="SciKick_logo.svg" width="16" height="28" alt="logo">

An AI research companion — Chrome extension + local server. Brainstorm ideas, discuss your scientific writing, analyze text-based data, and navigate peer review. Works with any scientific field.

[![Ko-fi](https://img.shields.io/badge/Ko--fi-Buy%20me%20a%20coffee-ff5e5b?logo=ko-fi)](https://ko-fi.com/scikick)

## What it does

- **Chat with your papers** — Discuss manuscripts, figure captions, text-based data, and reviewer feedback
- **Google Drive integration** — Load papers, figures, and documents directly from Drive
- **Cross-computer resume** — Session state saved to your Drive folder; pick up where you left off
- **Runs locally** — No hosting costs, your data stays on your machine
- **Cloud or local LLMs** — Use a hosted provider (Claude, GPT, Gemini, DeepSeek, GLM, Kimi) or run a model on your own machine via Ollama, LM Studio, or MLX — fully private, no API key needed for local
- **Scan or keep any document** — One-shot `scan this file` for a single question, or `keep` a file in context across every turn. Bring in the supplement, protocols, or reviewer PDFs alongside your manuscript
- **Live context meter** — See how much of the model's context window your next request will use, updated as you scan and keep documents
- **Streaming responses** — Real-time AI chat with streaming

### 📹 Feature Demo Video

<p align="center">
  <a href="https://www.youtube.com/watch?v=F5u4WGnunSs">
    <img src="https://img.youtube.com/vi/F5u4WGnunSs/0.jpg" alt="Feature Demo" width="480">
  </a>
</p>

### Limitations

- **Figures and images are not automatically analyzed** — SciKick extracts text from your files, not images. The AI can discuss figures via their captions and surrounding text, but cannot "see" graphs, microscopy images, or charts embedded in your documents.
- **Manual workaround** — You can paste screenshots of figures directly into the chat for visual analysis. This works with multi-modal LLMs like **Claude** (Sonnet 4, Opus 4, Fable 5) and **GPT-4o**.
- **Future plans** — If enough people ask for it, we'll add automatic figure extraction and parsing from PDFs and DOCX files. Let us know!

## Side Panel Overview

The top bar has five buttons (left to right):

| Button | Name | What it does |
|--------|------|--------------|
| **ℹ** | Info | View loaded data — project file tree, scraped articles, session state, memory stats. You can delete individual scraped articles or unload the entire project from here. |
| **—** | Clear Chat | Wipes the chat history shown on screen. Your project context and session memory are unaffected — the AI still remembers everything. |
| **🌙** | Theme | Toggle between dark theme (default) and light theme. Your preference is saved and persists across restarts. |
| **⚙** | Settings | Configure your LLM provider, API key, model, and custom base URL. Changes take effect immediately and are saved for the next restart. |
| **⟳** | Restart | Restarts your session — wipes the chat and re-shows the onboarding options ("What would you like to work on today?"). Server state is cleared but project files stay loaded. |

## Architecture

```
Chrome Extension (side panel) ↔ Local Server (localhost:8742) ↔ Google Drive API + LLM API
```

- **Server**: Python/FastAPI
- **Extension**: Chrome Manifest V3 side panel
- **Memory**: `.scikick_memory.json` stored in your Google Drive project folder
- **AI**: Multi-provider — Anthropic Claude, DeepSeek, GLM, OpenAI, Gemini, Kimi, any OpenAI-compatible API, or local LLMs (Ollama / LM Studio / MLX)

---

## Quick Start

### Prerequisites

- **Python 3.10+**
- **Chrome** (or Chromium-based browser: Edge, Brave, Arc)
- **An LLM** — either an API key from a supported cloud provider, **or** a local model via Ollama/LM Studio/MLX (no key needed; see below)
- **Google account** (any Gmail) — for Google Drive access

### Supported LLM Providers

| Provider | Get API key / install at | Default model |
|----------|--------------------------|---------------|
| **Anthropic (Claude)** | [console.anthropic.com](https://console.anthropic.com/) | `claude-sonnet-4-6` |
| **DeepSeek** | [platform.deepseek.com](https://platform.deepseek.com/) | `deepseek-v4-pro` |
| **Zhipu AI (GLM)** | [open.bigmodel.cn](https://open.bigmodel.cn/) | `glm-4-plus` |
| **OpenAI (GPT-4o)** | [platform.openai.com](https://platform.openai.com/) | `gpt-4o` |
| **Google (Gemini)** | [aistudio.google.com](https://aistudio.google.com/) (free tier available) | `gemini-2.0-flash` |
| **Moonshot AI (Kimi)** | [platform.moonshot.cn](https://platform.moonshot.cn/) | `moonshot-v1-128k` |
| **Local — Ollama** | [ollama.com](https://ollama.com/) (install) | `llama3.1` |
| **Local — LM Studio** | [lmstudio.ai](https://lmstudio.ai/) (install) | (whatever model is loaded in the GUI) |
| **Local — MLX Server** | [github.com/ml-explore/mlx-examples](https://github.com/ml-explore/mlx-examples) (install) | `mlx-community/Llama-3.1-8B-Instruct-4bit` |
| **Custom** (Groq, Together, etc.) | Your provider | Any |

> 💡 Want support for a specific AI provider? Open an issue or start a discussion on GitHub — we can usually add it quickly.
>
> 🏠 **Local LLMs are fully supported** — Ollama, LM Studio, and MLX Server are first-class options in the ⚙ Settings dropdown. No API key required (SciKick fills a placeholder the runtime ignores). See [Local LLM setup](#-local-llm-setup) below.

#### 🏠 Local LLM setup

SciKick can chat with a model running entirely on your machine — fully private, no API key, no per-token cost. Great for drafting, summarizing, and offline work.

1. **Install a runtime and pull a model** (Ollama is the easiest):
   ```bash
   brew install ollama
   ollama serve            # starts the server on :11434
   ollama pull gemma3:27b  # or qwen2.5:14b, llama3.1, phi4, etc.
   ```
   LM Studio and MLX Server work too — just start their built-in local server.
2. **Open the SciKick side panel → ⚙ Settings.**
3. **Pick your local runtime** (Ollama / LM Studio / MLX) from the Provider dropdown. The Base URL is prefilled (`http://localhost:11434/v1` for Ollama, `:1234/v1` for LM Studio, `:8080/v1` for MLX) — edit it if your runtime uses a different port.
4. **Set the model name** (e.g. `gemma3:27b`) and click **Save & Apply.** The API key field is hidden — leave it blank.

> ⚠️ **Quality note:** Even strong local models (e.g. Gemma 3 27B) generally don't match Claude Sonnet / GPT-4-class cloud models on nuanced scientific revision. Use local for drafting, summarizing, and privacy; switch back to a cloud provider for heavy restructuring. **RAM rule of thumb** (4-bit quantized): ~1 GB per 7B params — a 32 GB machine comfortably runs up to ~14B, and can squeeze a 27B.

### 1. Get the code

```bash
git clone https://github.com/JHCCoder/scikick.git
cd scikick
```

Or copy the folder from a USB stick / shared drive — no git required.

### 2. Run the setup wizard

```bash
./start.sh --setup
```

The wizard walks you through:
- Setting up Google Drive access (auto-opens each Google Cloud Console page for you, scans your Downloads for the credentials file, validates everything)
- Optionally installing a background service so the server auto-starts on login

Your LLM provider and API key are configured separately — after the server boots, open the SciKick side panel and click ⚙ Settings.

**This takes ~5–10 minutes** — most of that is clicking buttons in Google Cloud Console tabs that the wizard opens for you.

### 3. Start the server

The setup wizard asks if you want the server to start automatically on login (recommended). If you said yes, you're done — just click the extension.

Otherwise, start it manually:

```bash
./start.sh
```

Or install the background service later:

```bash
./start.sh --install-service
```

You should see:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Server:  http://localhost:8742
  Health:  http://localhost:8742/health
  API docs: http://localhost:8742/docs
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 4. Load the Chrome extension

```bash
./install-extension.sh
```

This auto-detects your browser, opens the extensions page, and copies the extension folder path to your clipboard.

Or manually:
1. Go to `chrome://extensions/`
2. Enable **Developer mode** (toggle in top right)
3. Click **Load unpacked**
4. Select the `extension/` folder from this project

### 5. Authenticate with Google (first time only)

Visit [http://localhost:8742/drive/auth/url](http://localhost:8742/drive/auth/url) in your browser, sign in with your Google account, and grant the requested permissions.

### 6. Load a project and start chatting

1. Click the SciKick icon 📄 in your Chrome toolbar to open the side panel
2. Paste your Google Drive folder URL (or ID):
   ```
   https://drive.google.com/drive/folders/1abc123...
   ```
3. Click **Load Project**
4. Pick your interaction type (Brainstorming, Paper Discussion, etc.)

Ask questions like:
- "What are Reviewer 2's main concerns?"
- "Help me draft a response about Figure 3"
- "Does my Methods section address the concern about sample size?"
- "Compare what Reviewer 1 and Reviewer 2 said about the statistical analysis"
- "Help me rephrase this paragraph to be clearer"
- "Scan and keep the supplemental material, then review my Figure S18ex legend"
- "Scan the reviewer PDF — what are the main concerns?"

---

## Google Drive Setup (re-run anytime)

```bash
./start.sh --setup
```

The wizard sets up Google Drive access and offers to install the background service. (Your LLM provider/API key are configured in the extension's ⚙ Settings panel, not here.)

### What the wizard does

The setup wizard treats Google Cloud Console as part of the app — it auto-opens each page you need and tells you exactly what to click:

| Step | What it opens | What you do |
|------|--------------|-------------|
| 1 | Project creation page | Click "Create" |
| 2 | Drive API page | Click "Enable" |
| 3 | Sheets API page | Click "Enable" |
| 4 | OAuth consent screen | Fill in app name, add scopes, add test user |
| 5 | Credentials page | Create OAuth client ID → Download JSON |
| 6 | (Local) | Wizard auto-finds the JSON in ~/Downloads, validates it, installs it |

---

## Project Folder Structure

Your Google Drive folder should look like this:

```
/My Paper Revision/
├── manuscript.pdf               # Your paper (PDF, DOCX, or Google Doc)
├── figures/
│   ├── fig1_methodology.png
│   ├── fig2_main_results.png
│   └── fig3_supplementary.png
├── supplementary/
│   ├── supp_table1.xlsx
│   └── supp_methods.pdf
├── reviewer_comments/            # Reviewer feedback
│   ├── reviewer_1.pdf
│   ├── reviewer_2.pdf
│   └── combined_comments.docx
├── response_letter.md            # Your draft response (optional)
└── .scikick_memory.json  # Auto-created session state
```

You can also use a **Google Sheet** for reviewer comments — the system auto-detects columns like "Reviewer", "Comment", "Severity", and "Response".

---

## Sharing with Labmates

Each person needs their own setup — SciKick runs locally and uses personal API keys and Google credentials.

### For a labmate setting up from scratch

1. **Get the code**: `git clone https://github.com/JHCCoder/scikick.git` (or copy from a USB stick)
2. **Get an LLM**: Sign up at [DeepSeek](https://platform.deepseek.com/) (or Anthropic, OpenAI, etc.) for an API key — or run a local model via Ollama/LM Studio/MLX (no key needed; see [Local LLM setup](#-local-llm-setup))
3. **Run the setup wizard**: `./start.sh --setup` — it guides you through Google Cloud setup (Drive access) and the background service
4. **Start the server**: `./start.sh`
5. **Load the extension**: Run `./install-extension.sh` or follow the manual steps
6. **Authenticate**: Visit `http://localhost:8742/drive/auth/url`
7. **Load a project**: Paste a Google Drive folder URL and click Load Project

### Using the same Google Drive folder (collaborating)

If you want to work on the same paper together:
- Share the Google Drive folder with your labmate (via Google Drive's Share button)
- Each person uses their **own** Google OAuth credentials and LLM API key
- The `.scikick_memory.json` file syncs via Drive — you'll see each other's chat context
- Each person's LLM responses are independent (your API keys are separate)

### Cross-computer resume (same person, different machine)

Same as above — clone the code, run `./start.sh --setup` on the new machine (you'll need your Google credentials JSON again). Paste the same Drive folder ID and the server restores your session.

### Security

- **Never commit `google_credentials.json`, `google_token.json`, or `.env`** — they contain secrets
- `.gitignore` already excludes these
- Each person should use their own Google Cloud OAuth client and API key
- Local data (`~/.scikick/`) contains tokens — don't share that folder

---

## Configuration

### LLM Provider

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `anthropic` | Provider: `anthropic`, `deepseek`, `glm`, `openai`, `gemini`, `kimi`, `local-ollama`, `local-lmstudio`, `local-mlx`, or `custom` |
| `LLM_API_KEY` | (required for cloud) | Your API key — not needed for local providers |
| `LLM_MODEL` | (provider default) | Model name override |
| `LLM_BASE_URL` | (provider default) | Base URL — preset for local runtimes, required for `custom` |

These are saved to `.env` by the setup wizard.

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `REVISION_HOST` | `127.0.0.1` | Server bind address |
| `REVISION_PORT` | `8742` | Server port |
| `GOOGLE_CREDENTIALS` | `~/.scikick/google_credentials.json` | Path to Google OAuth credentials |

### Switching providers later

Run `./start.sh --setup` again, or edit `.env` directly and restart the server.

---

## Commands

| Command | What it does |
|---------|-------------|
| `./start.sh` | Start the server |
| `./start.sh --setup` | Setup wizard (Google Drive + background service) |
| `./start.sh --drive` | Add Google Drive access (runnable while the server runs) |
| `./start.sh --install-service` | Install as background service (auto-start on login) |
| `./start.sh --uninstall-service` | Remove background service |
| `./start.sh --install` | Install dependencies then start |
| `./start.sh --help` | Show help |
| `./install-extension.sh` | Browser-guided extension loading |

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Server health check |
| `/drive/auth/url` | GET | Get Google OAuth URL |
| `/drive/auth/status` | GET | Check auth status |
| `/drive/folder/{id}/resume` | GET | Load project + restore memory |
| `/drive/folder/{id}/files` | GET | List files in folder |
| `/drive/file/{id}/download` | GET | Download a file |
| `/chat/send` | POST | Send message (SSE streaming) |
| `/chat/send-sync` | POST | Send message (synchronous) |
| `/chat/providers` | GET | List available/configured LLM providers |
| `/chat/context` | GET | Get current project context |
| `/memory/init` | POST | Initialise new memory |
| `/memory/status` | GET | Get memory status |
| `/memory/update` | POST | Update after chat turn |
| `/memory/decision` | POST | Record a decision |
| `/memory/comment/{id}` | PUT | Update a comment's state |

---

## Scanning & Keeping Documents

SciKick always has your **manuscript** loaded — the main document it auto-detects in your Drive folder (it picks "main submission" over "supplemental material" when both are present). For everything else — the supplement, a protocol, a reviewer PDF, a contamination report — you decide what to bring into the conversation, and for how long.

### One-shot scan — for a single question

Say `scan this file`, `scan this document`, or `scan <filename>` to pull a file's full text into context **for that one turn only**. Use it for a quick peek:

- "scan this document and review my Figure S18ex legend"
- "scan the reviewer PDF — what are the main concerns?"

The content is sent to the model for that request and then dropped — follow-up questions won't see it unless you scan again. The parsed text is cached, so re-scanning the same file is instant (no re-download). You can name a file naturally ("scan the supplemental material", "scan the reviewer comments") — SciKick matches it to a file in your Drive folder by distinctive name tokens, so you don't need to type the exact underscored filename.

### Keep — for a whole line of questioning

Say `scan and keep <filename>`, `keep this document loaded`, or `also load <filename>` to keep a file in context **every turn** until you remove it. Use it when you'll ask a series of follow-ups about a file:

- "scan and keep the supplemental material"
- "keep this document loaded"

Kept files persist across all your subsequent questions. Keep as many as you like — they stay until you remove them or switch projects. The ℹ Info panel lists everything currently kept under 📌 **Loaded Documents**.

You can also **click any file in the ℹ Info panel's Project Data tree** to scan-and-keep it — no phrasing needed. Hover a file to see the "📌 keep" hint.

When you're viewing a project file in your browser tab, a **📌 Scan** button appears in the tab bar — click it to scan-and-keep the file you're currently looking at.

Remove a file with:
- `remove this document` — the file open in your browser tab
- `remove <filename>` — a specific kept file
- `clear all loaded documents` — drop everything kept

> **At most one project at a time.** Loading a different Drive folder replaces your context and drops any kept files from the previous project. Within a folder, you can scan and keep any number of files.

### The context meter

The `% free` bar at the bottom estimates **how much of the model's context window the next request will consume** — the actual system prompt plus everything sent that turn (history, retrieved chunks, kept docs, one-shot scans). It mirrors how tools like Claude Code report context.

- A **one-shot scan** raises the bar for that request, then it drops back on your next message (the file isn't resent). This is expected, not a bug.
- A **kept file** raises the bar and keeps it raised, since it's resent every turn.
- If you keep a file and then also scan it, it's only counted once (deduplicated by file).

Before your first message of a session the bar shows a rough projection; after the first message it reflects the real request size.

### Notes on extraction

- Text and figure legends are extracted from `.docx`/`.pdf`. In `.docx`, **table cell text is also extracted** (rendered as markdown-style tables, in document order). PDF tables are not reconstructed — only flowed text is read.
- Very large files are capped per document, but the cap **scales with your model's context window** (roughly half the window for a one-shot scan, a quarter for a kept doc). On a 1M-token model a typical manuscript or supplement loads in full; on a 128k-token model the cap is ~256k characters. If a file exceeds the cap, only the first portion is loaded and the model will tell you it was truncated and suggest asking about a specific section for the rest.
- Kept text is frozen at keep-time. If you edit a kept file on Drive, click **Load Project** (same folder — it preserves your kept list) and re-keep that file to refresh it.

---

## Tips & Troubleshooting

### Tips

- **Use a Google Sheet for reviewer comments** — easier to track status and add draft responses
- **Name figures clearly** — the AI reads captions and surrounding text, so `fig2_main_results.png` gives more context than `IMG_4829.png`
- **Scan vs. keep** — `scan this file` brings a file in for one question; `scan and keep <file>` keeps it for every follow-up. Use keep when you'll ask a series of questions about a document.
- **Keep the server running** — it's lightweight and stateless between requests
- **The memory file is human-readable** — you can inspect or edit `.scikick_memory.json` in your Drive folder

### Troubleshooting

**Server won't start — "No LLM API key found"**
Run `./start.sh --setup` to configure your API key, or create a `.env` file with `LLM_API_KEY=your-key-here`.

**"Google credentials not found" when starting**
Run `./start.sh --setup` to run the guided Google Drive setup wizard.

**Extension side panel shows "Server disconnected"**
Make sure the server is running (`./start.sh`). The status dot in the top bar should be green.

**Google auth page says "Error: redirect_uri_mismatch"**
Make sure you created an OAuth client ID of type "Desktop application" (not "Web application"). Re-run `./start.sh --setup` to redo the credentials.

**"This app isn't verified" warning during Google sign-in**
This is normal for a local app. Click "Advanced" → "Go to SciKick (unsafe)" to continue. You added yourself as a test user during setup, so this works.

**Local LLM not responding / "connection refused"**
Make sure your runtime is running (`ollama serve` for Ollama) and the Base URL in ⚙ Settings matches the port — `11434` for Ollama, `1234` for LM Studio, `8080` for MLX. Also confirm you've pulled/loaded a model and that the model name in Settings matches exactly (e.g. `gemma3:27b`, including the tag).

---

## Support, Stars & License

If SciKick saves you time on your research, [buy me a coffee on Ko-fi](https://ko-fi.com/scikick) ☕.

<p align="center">
  <a href="https://www.star-history.com/?repos=JHCCoder%2FSciKick&type=date&legend=top-left">
    <img src="https://api.star-history.com/svg?repos=JHCCoder/SciKick&type=Date" alt="Star History Chart">
  </a>
  <br>
  <a href="https://github.com/JHCCoder/scikick/stargazers">
    <img src="https://img.shields.io/github/stars/JHCCoder/scikick?style=social" alt="GitHub stars">
  </a>
</p>

MIT
