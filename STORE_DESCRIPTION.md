# Chrome Web Store — Store Listing Description

This is the canonical copy for the Web Store listing **Description** field. Paste the content below the `---` into the dashboard's Description box (from `# SciKick — Your AI Research Sidekick` to the end). It can be edited in the dashboard at any time — **no new extension version upload is required** to change listing text.

The project-memory file is `.scikick_memory.json` (lowercase, dot-prefixed) — do **not** refer to it as `SciKickMemory.json`.

> ⚠️ **Keyword-spam policy (learned 2026-09-03):** the Chrome Web Store rejects Descriptions that enumerate AI-provider brand names (Claude/GPT/Gemini/DeepSeek/GLM/Kimi/Grok/MiniMax/Qwen) as Keyword Spam. Do NOT add a provider-brand list to this file — describe the function instead ("major AI providers", "any OpenAI-compatible endpoint", "supported reasoning-capable models"). The full provider list lives in the ⚙ Settings dropdown + README, not the store Description. See `store-keyword-spam-policy` memory.

---

# SciKick — Your AI Research Sidekick

SciKick is a context-aware AI assistant built for scientific research. It brings your manuscripts, supplemental material, reviewer comments, figures, and selected research articles into a single workspace where you can ask questions, improve your writing, plan revisions, identify knowledge gaps, and move faster — using your choice of local or cloud AI models with your own API key.

Unlike a general-purpose AI chatbot, SciKick understands your entire research project. Instead of repeatedly copying and pasting documents into a chat window, point SciKick to your project folder once. For every question, it retrieves the most relevant sections from your manuscripts, supporting documents, and literature, giving you context-aware answers throughout the research lifecycle — from first draft through peer review.

## How it works

1. **Point SciKick to your Google Drive project folder.** It reads your manuscripts, supplements, reviewer comments, figures, and documents.
2. **Set a goal** — paper revision, paper writing, grant, application, or open brainstorming. SciKick tailors its guidance to that purpose, including journal-specific formatting notes (word/figure limits, abstract structure, citation style).
3. **Ask anything.** SciKick retrieves the relevant context per question and answers with your full project in mind.
4. **Scan or keep documents** as needed — one-shot "scan this file" for a single question, or "keep" a file in context across every turn. A live context meter shows how much of the model's window your next request will use.

## What SciKick can do

Whether you're preparing a new manuscript or responding to peer review, SciKick helps you:

- Brainstorm research ideas and experimental directions
- Discuss manuscripts and improve scientific writing
- Analyze manuscripts, supplemental material, reviewer comments, and other research documents
- Import and analyze selected online research articles (including PDFs) alongside your project files
- Ask questions about your research and receive context-aware answers
- Identify missing information, knowledge gaps, and opportunities to strengthen your manuscript
- Recommend what to add, where to add it, and which document should be updated
- Plan revisions and organize next steps
- Draft and refine responses to reviewers
- Summarize documents and literature
- Maintain continuity across long-term research projects
- Search your full conversation history with inline results
- Pin important messages to a top panel for quick reference

## Built for researchers

SciKick works directly with your Google Drive research folders, keeping manuscripts, figures, supplemental files, reviewer comments, and related documents organized in one place. When you load a project, SciKick asks what you're working toward — a manuscript revision, a grant, an application, or open brainstorming — and shapes its suggestions around that goal, with a quick recap each time you return.

To maintain continuity across sessions, SciKick automatically creates and updates a **.scikick_memory.json** file within your project folder. This project memory preserves important discussions, decisions, plans, and research context, allowing you — or SciKick running on another computer — to quickly pick up where you left off without losing momentum.

Need additional context? SciKick can import selected research articles from the web, including PDFs, allowing your AI assistant to reason over both your project documents and relevant literature in the same conversation.

## Choose your AI

You're in control of the AI model you use. SciKick connects to local language models or cloud providers through your own API credentials — bring your own key and choose the model that best fits your workflow, privacy requirements, and budget. It works with major AI providers and any OpenAI-compatible endpoint, or you can run a model entirely on your own machine for fully private, no-cost use. A smart **thinking toggle** (Auto / On / Off) gives you control over deep reasoning on supported reasoning-capable models, so you can favor depth on hard analytical questions and speed on routine ones.

## Why researchers choose SciKick

- Context-aware conversations across your entire research project
- Support for manuscripts, supplemental files, reviewer comments, figures, and research documents
- Import selected online research articles — including PDFs — for additional context
- Intelligent revision planning and manuscript improvement
- Recommendations for what to add and where to add it
- Persistent project memory through **.scikick_memory.json**
- Continue your work seamlessly across sessions and computers
- Support for major cloud and local AI providers — bring your own API key, or run a model locally for fully private, no-cost use
- Bring your own API key — no required subscriptions or vendor lock-in
- One-click refresh of kept documents from Google Drive as you edit them
- Automatic prefix caching — repeat turns on supported providers are faster and cheaper
- Smart thinking control (Auto / On / Off) on supported reasoning-capable models
- Conversation search and pinned messages
- Live context meter and per-document budgets that scale with your model's context window
- Project goals and modes (paper revision, grant, application, brainstorming) with journal/program formatting notes
- Optional scanned-PDF OCR for image-only supplements and reviewer letters
- Provider-keyed model dropdown for quick, guided setup
- Dark and light themes
- No third-party servers required for your research workflow — your data stays local
