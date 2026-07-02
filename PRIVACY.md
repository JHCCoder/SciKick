# SciKick Privacy Policy

_Last updated: July 2, 2026_

SciKick is a Chrome extension and local server that helps you brainstorm, write, and chat with your scientific papers. This policy explains what data is involved and where it goes.

## The short version

- SciKick does **not** require an account and does **not** collect, sell, or share your data.
- The SciKick server runs **on your own computer** (localhost). It is not a cloud service run by SciKick.
- Your paper content and chat messages are sent to the **LLM provider you configure** (for example Anthropic, OpenAI, DeepSeek, Zhipu AI, Google Gemini, or Moonshot AI) so the AI can respond. That provider's own privacy policy applies to anything you send it.
- Google Drive access uses **your own** Google Cloud project and OAuth credential — SciKick does not use a shared account or its own keys to read your Drive.

## What SciKick stores

**In your browser (Chrome's local extension storage):**
- Your chosen LLM provider, model, and API key
- The Google Drive folder ID you selected
- UI preferences (theme) and a flag remembering whether you've connected before

This stays in `chrome.storage.local` on your machine. It is not synced to any SciKick server.

**On your computer (the local server, `~/.scikick/`):**
- Your Google OAuth token (`google_token.json`) and the OAuth client credential you downloaded (`google_credentials.json`)
- A cache of files you've opened, so the extension can respond quickly

**In your Google Drive:**
- A memory file (`.scikick_memory.json`) inside the folder you chose, which stores your project notes and chat history so you can resume across machines. This file lives in **your** Drive; SciKick cannot read it except through your own OAuth grant.

## What is processed and where it goes

When you use SciKick, the following data flows happen:

1. **Scrape this page** — when you click "Scrape this page," SciKick reads the HTML of the tab you're viewing and sends it to your local server (localhost:8742). The server extracts the paper text and keeps it in memory for your chat session.
2. **Chat** — your messages, along with the paper or scraped content currently loaded, are sent from the local server to the LLM provider you configured, and the provider's reply is shown in the side panel.
3. **Google Drive** — with your authorization, SciKick reads manuscripts and reviewer comments from the Drive folder you selected, and writes the memory file there.

Paper content and chat history are held in the local server's memory until you reset the session or stop the server. The project memory is persisted to your Drive until you delete it.

## Third-party services

SciKick itself does not run any servers that receive your data. The only third parties that receive data are ones you choose:

- **Your LLM provider.** When you chat or scrape a paper, that content is sent to the provider whose API key you entered, for the purpose of generating AI responses. Each provider has its own privacy policy. You are responsible for understanding how your chosen provider handles data.
- **Google Drive.** Used only if you connect it, only through your own Google account, and only for the folder you select.

No analytics, advertising, or tracking SDKs are included in SciKick.

## Your choices

You can delete your data at any time:

- **Extension data:** remove the SciKick extension from Chrome, or clear its storage via Chrome's extension settings.
- **Local server data:** delete the `~/.scikick/` directory.
- **Drive memory file:** delete `.scikick_memory.json` from your chosen Drive folder.
- **Google access:** revoke SciKick's access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

Disconnecting Drive or removing the extension stops further data flow immediately.

## Security

Your LLM API key and Google OAuth token are stored locally on your machine and are transmitted only to the services they authenticate (your LLM provider and Google, respectively). SciKick does not transmit credentials to itself or any intermediary. Because the server runs locally and binds to `127.0.0.1`, it is not reachable from other machines on your network by default.

## Children

SciKick is not directed at children under 13 (or the equivalent age in your jurisdiction) and is not intended for use by them.

## Changes

If this policy changes, the updated version will be posted in this repository with a revised date.

## Contact

For privacy questions or requests, please open an issue at [github.com/JHCCoder/SciKick](https://github.com/JHCCoder/SciKick) or email [jhc103@ucsd.edu](mailto:jhc103@ucsd.edu).
