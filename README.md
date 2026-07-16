# <img src="SciKick_logo.svg" width="16" height="28" alt="logo"> SciKick <img src="SciKick_logo.svg" width="16" height="28" alt="logo">

**SciKick is a context-aware AI assistant for scientific research.** It brings together your manuscripts, supplemental material, reviewer comments, research documents, and selected literature into a single workspace where you can ask questions, improve writing, plan revisions, identify gaps, and accelerate your research using local or cloud AI models.

[![Ko-fi](https://img.shields.io/badge/Ko--fi-Buy%20me%20a%20coffee-ff5e5b?logo=ko-fi)](https://ko-fi.com/scikick)
[![Chrome Web Store](https://img.shields.io/badge/Chrome%20Web%20Store-Install%20SciKick-4285F4?logo=googlechrome&logoColor=white)](https://chromewebstore.google.com/search/SciKick)

> **Easiest install:** get SciKick from the [Chrome Web Store](https://chromewebstore.google.com/search/SciKick) — no build step required. The [Quick Start](#quick-start) below is for building from source or running a development build.

---

## What SciKick does

Unlike a general-purpose AI chatbot, SciKick understands your entire research project. Instead of repeatedly copying and pasting documents into an AI assistant, simply point SciKick to your project folder. It retrieves the most relevant information from your manuscripts, supporting documents, and literature for every conversation, giving you context-aware answers throughout the research lifecycle.

Whether you're preparing a new manuscript or responding to peer review, SciKick helps you:

- **Brainstorm** research ideas and experimental directions
- **Discuss manuscripts** and improve scientific writing
- **Analyze** manuscripts, supplemental material, reviewer comments, and other research documents
- **Import online research articles** from the web and reason over them alongside your project files
- **Ask questions** about your research and receive context-aware answers
- **Identify gaps** — missing information, knowledge gaps, and opportunities to strengthen your manuscript
- **Recommend** what to add, where to add it, and which document should be updated
- **Plan revisions** and organize next steps
- **Draft and refine** responses to reviewers
- **Summarize** documents and literature
- **Maintain continuity** across long-term research projects

### Key features

- **Context-aware chat** — retrieves the most relevant sections of your paper per question, so you get smarter answers without burning through your context window
- **Scan or keep any document** — one-shot `scan this file` for a single question, or `keep` a file in context across every turn. Bring in the supplement, protocols, or reviewer PDFs alongside your manuscript
- **Live context meter** — see how much of the model's context window your next request will use, updated as you scan and keep documents
- **Import web articles** — scrape a journal article from any website with one click and analyze it next to your project files
- **Google Drive integration** — load papers, figures, and documents directly from Drive
- **Cross-computer resume** — project memory saved to your Drive folder; pick up where you left off from any machine
- **Cloud or local LLMs** — use a hosted provider (Claude, GPT, Gemini, DeepSeek, GLM, Kimi), any OpenAI-compatible endpoint, or run a model on your own machine via Ollama, LM Studio, or MLX — fully private, no API key needed for local
- **Streaming responses** — real-time AI chat with streaming
- **Dark and light themes**
- **Runs entirely locally** — no third-party servers, no data collection

### 📹 Feature Demo Video

<p align="center">
  <a href="https://www.youtube.com/watch?v=F5u4WGnunSs">
    <img src="https://img.youtube.com/vi/F5u4WGnunSs/0.jpg" alt="Feature Demo" width="480">
  </a>
</p>

### Limitations

- **Figures and images are not automatically analyzed** — SciKick extracts text from your files, not images. The AI can discuss figures via their captions and surrounding text, but cannot "see" graphs, microscopy images, or charts embedded in your documents.
- **Manual workaround** — you can paste screenshots of figures directly into the chat for visual analysis. This works with multi-modal LLMs like **Claude** (Sonnet 4, Opus 4, Fable 5) and **GPT-4o**.
- **Future plans** — if enough people ask for it, we'll add automatic figure extraction and parsing from PDFs and DOCX files. Let us know!

---

## How it works

```
Chrome Extension (side panel) ↔ Local Server (localhost:8742) ↔ Google Drive API + LLM API
```

- **Server**: Python/FastAPI, runs on your machine
- **Extension**: Chrome Manifest V3 side panel
- **Memory**: `.scikick_memory.json` stored in your Google Drive project folder — preserves important discussions, decisions, and plans so you (or SciKick on another computer) can pick up where you left off
- **AI**: multi-provider — Anthropic Claude, OpenAI, DeepSeek, GLM, Gemini, Kimi, any OpenAI-compatible API, or local LLMs (Ollama / LM Studio / MLX). You bring your own API key or run a local model — no required subscriptions, no vendor lock-in.

---

## Quick Start

### Prerequisites

- **Python 3.10+**
- **Chrome** (or Chromium-based browser: Edge, Brave, Arc)
- **An LLM** — either an API key from a supported cloud provider, **or** a local model via Ollama/LM Studio/MLX (no key needed; see [Choosing your AI](#choosing-your-ai))
- **Google account** (any Gmail) — for Google Drive access

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
- "change goal" — re-run the project-goal setup (switch mode/journal/grant/application)

---

## Choosing your AI

You're in control of the AI model you use. SciKick connects to local language models or cloud providers through your own API credentials. Configure it in the extension's ⚙ Settings panel — changes take effect immediately and persist across restarts.

### Supported LLM providers

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

### Switching providers later

Run `./start.sh --setup` again, or edit `.env` directly and restart the server. (Provider/model/key set in the ⚙ Settings panel override `.env` at runtime.)

---

## Project Goals & Modes

The first time you load a project, SciKick asks **what you're working on** and tailors every conversation to that goal. The goal is saved per-project in `.scikick_memory.json`, so it carries across sessions and machines.

**Modes:**

| Mode | Follow-up asked | Why |
|------|-----------------|-----|
| 📝 Paper Revision / ✍️ Paper Writing | Target journal | SciKick looks up that journal's author guidelines (word/figure limits, abstract structure, citation style) + a source URL and factors them into revision/writing advice |
| 💰 Grant | Grant type (NIH, NSF, ERC, foundation, industry, other) | Looks up the grant mechanism / review criteria + source URL; tailors advice to its aims and structure |
| 🎓 Application | Application type (job, med school, grad school/PhD, other) → target | Looks up the target program's mission, prerequisites, and what it looks for + source URL |
| 🧠 Brainstorming / 📄 Paper Discussion / 💬 Other | None | General-purpose |

**On later loads**, SciKick recaps the saved goal (including the looked-up info and its source URL) and tells you to say **"change goal"** if anything is wrong — which re-runs the whole question pipeline and overwrites the saved goal.

> The lookup uses your configured AI's knowledge to pull together the target's key info **and a canonical source URL** (e.g. the journal's instructions-for-authors page, the program's admissions page). Journal/program websites are often JavaScript-rendered and can't be reliably scraped, so the AI draws on its training knowledge rather than live-scraping the page — always click the cited URL to verify specifics before submission. If the AI doesn't know a target, it says so and falls back to general advice.

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

## Importing Web Articles

Need to reason over a paper that isn't in your Drive folder? When you're viewing a journal article (or any web page) in your browser, the **Load this folder** button in the tab bar turns into **Scrape this page** — click it to import the article's text into the conversation.

Scraped articles accumulate for the session (you can import several) and are kept separate from your Drive project files. They appear under **Scraped Articles** in the ℹ Info panel, where you can delete individual ones. Combine them with your project: "compare this article's methods to my manuscript's Methods section."

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
└── .scikick_memory.json  # Auto-created project memory
```

You can also use a **Google Sheet** for reviewer comments — the system auto-detects columns like "Reviewer", "Comment", "Severity", and "Response".

---

## Side Panel Overview

The top bar has five buttons (left to right):

| Button | Name | What it does |
|--------|------|--------------|
| **ℹ** | Info | View loaded data — project file tree, scraped articles, session state, memory stats. You can delete individual scraped articles or unload the entire project from here. |
| **—** | Clear Chat | Wipes the chat history shown on screen. Your project context and session memory are unaffected — the AI still remembers everything. |
| **🌙** | Theme | Toggle between dark theme (default) and light theme. Your preference is saved and persists across restarts. |
| **⚙** | Settings | Configure your LLM provider, API key, model, and custom base URL. Changes take effect immediately and are saved for the next restart. |
| **⟳** | Restart | Restarts your session — wipes the chat and re-shows the onboarding options ("What would you like to work on today?"). Server state is cleared but project files stay loaded. |

---

## Configuration

### LLM Provider

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `anthropic` | Provider: `anthropic`, `deepseek`, `glm`, `openai`, `gemini`, `kimi`, `local-ollama`, `local-lmstudio`, `local-mlx`, or `custom` |
| `LLM_API_KEY` | (required for cloud) | Your API key — not needed for local providers |
| `LLM_MODEL` | (provider default) | Model name override |
| `LLM_BASE_URL` | (provider default) | Base URL — preset for local runtimes, required for `custom` |

These are saved to `.env` by the setup wizard. Values set in the ⚙ Settings panel override `.env` at runtime.

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `REVISION_HOST` | `127.0.0.1` | Server bind address |
| `REVISION_PORT` | `8742` | Server port |
| `GOOGLE_CREDENTIALS` | `~/.scikick/google_credentials.json` | Path to Google OAuth credentials |

---

## Commands

| Command | What it does |
|---------|--------------|
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
| `/chat/scrape` | POST | Import a web article into the session |
| `/chat/providers` | GET | List available/configured LLM providers |
| `/chat/context` | GET | Get current project context |
| `/memory/init` | POST | Initialise new memory |
| `/memory/status` | GET | Get memory status |
| `/memory/update` | POST | Update after chat turn |
| `/memory/decision` | POST | Record a decision |
| `/memory/comment/{id}` | PUT | Update a comment's state |

---

## Sharing with Labmates

Each person needs their own setup — SciKick runs locally and uses personal API keys and Google credentials.

### For a labmate setting up from scratch

1. **Get the code**: `git clone https://github.com/JHCCoder/scikick.git` (or copy from a USB stick)
2. **Get an LLM**: sign up at [DeepSeek](https://platform.deepseek.com/) (or Anthropic, OpenAI, etc.) for an API key — or run a local model via Ollama/LM Studio/MLX (no key needed; see [Local LLM setup](#-local-llm-setup))
3. **Run the setup wizard**: `./start.sh --setup` — it guides you through Google Cloud setup (Drive access) and the background service
4. **Start the server**: `./start.sh`
5. **Load the extension**: run `./install-extension.sh` or follow the manual steps
6. **Authenticate**: visit `http://localhost:8742/drive/auth/url`
7. **Load a project**: paste a Google Drive folder URL and click Load Project

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

## Tips & Troubleshooting

### Tips

- **Use a Google Sheet for reviewer comments** — easier to track status and add draft responses
- **Name figures clearly** — the AI reads captions and surrounding text, so `fig2_main_results.png` gives more context than `IMG_4829.png`
- **Scan vs. keep** — `scan this file` brings a file in for one question; `scan and keep <file>` keeps it for every follow-up. Use keep when you'll ask a series of questions about a document.
- **Keep the server running** — it's lightweight and stateless between requests
- **The memory file is human-readable** — you can inspect or edit `.scikick_memory.json` in your Drive folder

### Troubleshooting

**Server won't start — "No LLM API key found"**
Run `./start.sh --setup` to configure your API key, or create a `.env` file with `LLM_API_KEY=your-key-here`. (Local providers don't need a key.)

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
