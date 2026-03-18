# TPM Month-End Delivery Tool

A local web app built for [The Prosperity Maven](https://theprosperitymaven.ca) to automate the monthly client report delivery workflow.

## What it does

- Select a client from a dropdown (loaded from a local config file)
- Upload PDF reports directly to the correct Google Drive folder (auto-creates year + reporting subfolders)
- Paste a Cap.so video transcript — Claude AI extracts 2–3 plain-English financial highlights
- Generates a branded Gmail draft with the highlights, video link, Drive links, and your Gmail signature
- Handles duplicate file detection, missing folder warnings, and quarterly vs. monthly reporting folders

## Tech stack

- **Backend:** Python / Flask
- **AI:** Anthropic Claude API (claude-haiku) for callout extraction
- **Integrations:** Google Drive API v3, Gmail API v1
- **Auth:** Google OAuth2 (desktop app flow)
- **Frontend:** Vanilla HTML/CSS/JS — no frameworks

## Setup

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in your API keys
3. Add your `client_secret_*.json` from Google Cloud Console (OAuth desktop app credentials)
4. Add your clients to `client_config.json`
5. Run `bash run.sh` — it will create a venv, install dependencies, and launch the app
6. On first run, a browser window will open for Google OAuth authorization

## Running locally

```bash
bash run.sh
```

The app opens at `http://localhost:8080`.
