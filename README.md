# 📄 PhiDkick

An AI research companion — Chrome extension + local server. Brainstorm ideas, discuss your scientific writing, analyze data, and navigate peer review. Works with any scientific field.

## What it does

- **Chat with your papers** — Discuss manuscripts, figures, data, and reviewer feedback
- **Google Drive integration** — Load papers, figures, and documents directly from Drive
- **Cross-computer resume** — Session state saved to your Drive folder; pick up where you left off
- **Runs locally** — No hosting costs, your data stays on your machine
- **Streaming responses** — Real-time AI chat with streaming

## Architecture

```
Chrome Extension (side panel) ←→ Local Server (localhost:8742) ←→ Google Drive API + Claude API
```

- **Server**: Python/FastAPI, runs on your Mac
- **Extension**: Chrome Manifest V3 side panel
- **Memory**: `.paper-assistant-memory.json` stored in your Google Drive project folder
- **AI**: Anthropic Claude (streaming via SSE)

## Quick Start

### Prerequisites

- **Python 3.10+** (macOS comes with Python 3)
- **Chrome or Chromium** browser
- **LLM API key** — from one of the supported providers (see below)
- **Google Cloud project** with Drive API enabled (for Google Drive access)

### Supported LLM Providers

| Provider | Env vars | Default model |
|----------|----------|---------------|
| **Anthropic (Claude)** | `LLM_API_KEY` or `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` |
| **DeepSeek** | `LLM_API_KEY` or `DEEPSEEK_API_KEY` | `deepseek-chat` |
| **OpenAI (GPT-4o)** | `LLM_API_KEY` or `OPENAI_API_KEY` | `gpt-4o` |
| **Custom** (Ollama, Groq, Together, etc.) | `LLM_API_KEY` + `LLM_BASE_URL` | Any |

Set the provider via:
```bash
export LLM_PROVIDER=deepseek        # or anthropic, openai, custom
export LLM_API_KEY=sk-...           # your API key
export LLM_MODEL=deepseek-chat      # optional, defaults shown above
```

### 1. Setup

```bash
cd scientific-paper-assistant
./start.sh --setup
```

The setup wizard will:
- Let you choose your LLM provider (Anthropic, DeepSeek, OpenAI, or custom)
- Prompt for the corresponding API key
- Let you pick a model name
- Guide you through Google Cloud credential setup

### 2. Start the server

```bash
./start.sh
```

You should see:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Server:  http://localhost:8742
  Health:  http://localhost:8742/health
  API docs: http://localhost:8742/docs
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 3. Load the Chrome extension

1. Go to `chrome://extensions/`
2. Enable **Developer mode** (toggle in top right)
3. Click **Load unpacked**
4. Select the `extension/` folder from this project
5. The extension icon appears in your toolbar

### 4. Authenticate with Google

First time only:
1. Visit `http://localhost:8742/drive/auth/url` in your browser
2. Sign in with your Google account
3. Grant Drive (read-only) and Sheets (read-only) access
4. You'll be redirected — authentication is complete

### 5. Load your project

1. Click the extension icon to open the side panel
2. Paste your Google Drive folder URL (or ID) in the input field
   ```
   https://drive.google.com/drive/folders/1abc123...
   ```
3. Click **Load Project**
4. If this is a resumed session, you'll see a summary of where you left off

### 6. Start chatting

Ask questions like:
- "What are Reviewer 2's main concerns?"
- "Help me draft a response about Figure 3"
- "Does my Methods section address the concern about sample size?"
- "Compare what Reviewer 1 and Reviewer 2 said about the statistical analysis"
- "Help me rephrase this paragraph to be clearer"

## Project Folder Structure

Your Google Drive folder should look like this:

```
/My Paper Revision/
├── manuscript.pdf               # Your paper (PDF or DOCX or Google Doc)
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
└── .paper-assistant-memory.json         # Auto-created session state
```

You can also use a **Google Sheet** for reviewer comments — the system auto-detects columns like "Reviewer", "Comment", "Severity", and "Response".

## Sharing with Labmates

Each person needs their own setup — PhiDkick runs locally and uses personal API keys and Google credentials.

### For a labmate setting up from scratch

1. **Get the code**: Clone or copy the `scientific-paper-assistant/` folder
2. **Get an LLM API key**: Sign up at [DeepSeek](https://platform.deepseek.com/) (or Anthropic, OpenAI, etc.) and get an API key
3. **Set up Google Cloud**:
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a project → Enable **Google Drive API** and **Google Sheets API**
   - Create an OAuth 2.0 Client ID (Desktop application)
   - Download the credentials JSON → save as `~/.scientific-paper-assistant/google_credentials.json`
4. **Run setup**: `./start.sh --setup` — configures LLM provider and API key
5. **Start the server**: `./start.sh`
6. **Load the extension**: `chrome://extensions` → Developer mode → Load unpacked → select the `extension/` folder
7. **Authenticate**: Visit `http://localhost:8742/drive/auth/url` → sign in with Google
8. **Load a project**: Paste a Google Drive folder URL and click Load Project

### Using the same Google Drive folder (collaborating)

If you want to work on the same paper together:
- Share the Google Drive folder with your labmate (via Google Drive's Share button)
- Each person uses their **own** Google OAuth credentials and LLM API key
- The `.paper-assistant-memory.json` file syncs via Drive — you'll see each other's chat context
- Each person's LLM responses are independent (your API keys are separate)

### Cross-computer resume (same person, different machine)

Same as above — clone the code, copy your `google_credentials.json`, run setup. Paste the same Drive folder ID and the server restores your session.

### Security

- **Never commit `google_credentials.json`, `google_token.json`, or `.env`** — they contain secrets
- `.gitignore` already excludes these
- Each person should use their own Google Cloud OAuth client and API key
- Local data (`~/.scientific-paper-assistant/`) contains tokens — don't share that folder

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

## Configuration

### LLM Provider

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `anthropic` | Provider: `anthropic`, `deepseek`, `openai`, or `custom` |
| `LLM_API_KEY` | (required) | Your API key (unified; provider-specific keys also accepted) |
| `LLM_MODEL` | (provider default) | Model name override |
| `LLM_BASE_URL` | (provider default) | Base URL for openai/custom providers |

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `REVISION_HOST` | `127.0.0.1` | Server bind address |
| `REVISION_PORT` | `8742` | Server port |
| `GOOGLE_CREDENTIALS` | `~/.scientific-paper-assistant/google_credentials.json` | Path to Google OAuth credentials |

### Switching providers later

Just change `LLM_PROVIDER` and (if needed) `LLM_API_KEY` + `LLM_MODEL` in your `.env` file or shell, then restart the server. All chat history and reviewer comment state are preserved.

## Tips

- **Use a Google Sheet for reviewer comments** — easier to track status and add your draft responses
- **Name figures clearly** — `fig2_main_results.png` is better than `IMG_4829.png`
- **Keep the server running** — it's lightweight and stateless between requests
- **The memory file is human-readable** — you can inspect or edit `.paper-assistant-memory.json` in your Drive folder

## License

MIT
