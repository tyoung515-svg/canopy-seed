# Canopy Seed

**Describe what you want. AI agents build it.**

Canopy Seed is an automated AI build pipeline. You tell it what you want to build in plain English, and a team of AI agents designs, codes, tests, debugs, and delivers a working project — no coding experience needed.

This is an **early access release**. Things will break. That's the point — we need your help finding bugs and testing features before a wider launch. If something goes sideways, open an issue and let us know.

---

## What Actually Happens When You Use It

1. **You describe your idea.** "A recipe tracker that lets me search by ingredient" or "an inventory system for my shop." Just talk naturally.

2. **Big Brain asks you questions.** An AI conducts a short conversation to understand what you actually need — who uses it, what matters most, what it should look like. You can paste screenshots or sketches if you have them.

3. **The pipeline builds it.** Once your idea is fleshed out, Canopy hands it to a team of specialized AI agents:
   - An **Orchestrator** breaks the project into tasks
   - A **Dev Swarm** writes the code (multiple agents working in parallel)
   - **Giant Brain** reviews everything for quality
   - **Big Fixer** runs tests and fixes failures automatically
   - The **Manager** does final debugging — it can restart your app, take screenshots, read console logs, and fix issues on its own

4. **You get a working project.** Exported to a folder, ready to run.

---

## Quick Start

### Requirements

- **Python 3.11+** — Download from [python.org](https://python.org) if you don't have it. During install on Windows, check "Add Python to PATH."
- **An API key** for at least one of these:
  - **Google Gemini** — The build pipeline runs on Gemini. Get a free key at [aistudio.google.com](https://aistudio.google.com)
  - **Anthropic Claude** — Used for escalation on tough problems. Get a key at [console.anthropic.com](https://console.anthropic.com)
  - **Local models** via LM Studio or Ollama — Free, runs on your GPU, no API key needed

### Install

**Windows:** Double-click `install.bat`. It handles everything — Python check, virtual environment, dependencies, Playwright browser (used for screenshots during debugging), and creates a launch shortcut.

**Mac / Linux:**
```bash
bash install.sh
```

That's it. The installer will tell you when it's done.

### Run

**Windows:** Double-click `START CANOPY.bat`

**Mac / Linux:**
```bash
./start.sh
```

Your browser opens automatically. If this is your first time, you'll see the Vault Setup screen — enter your API key(s) there. They're encrypted (AES-256-GCM) and stored locally in `memory/vault.enc`. Your keys never touch a config file or leave your machine.

### Where Things Live

Once running, you'll find Canopy at:

- **Main UI:** http://localhost:7822
- **Hub (launch built apps):** http://localhost:7822/hub
- **DevHub (settings, vault, tools):** http://localhost:7822/devhub

---

## How the Build Pipeline Works

This is a simplified version of what happens under the hood:

```
Your Idea
    |
    v
[ Contract Phase ] --- Big Brain asks questions, researches, writes a spec
    |
    v
[ Orchestrator ] ------ Breaks the spec into subtasks
    |
    v
[ Dev Swarm ] --------- Multiple AI agents write code in parallel
    |
    v
[ Giant Brain ] ------- Reviews all code for quality and coherence
    |
    v
[ Big Fixer ] --------- Runs tests, auto-fixes failures (up to 20 rounds)
    |
    v
[ Manager ] ----------- Final debugging: restarts app, screenshots, console logs
    |
    v
Working Project in exports/
```

Each stage is autonomous — the agents use tools (file read/write, search, HTTP requests, browser screenshots) to do their work without needing your input. You can watch the progress in real time from the Hub.

---

## Early Access — What to Expect

This is a **pre-release build** meant for testing. Here's the honest truth about where things stand:

**What works well:**
- Full build pipeline from idea to working app
- Encrypted API key vault (your keys are safe)
- Manager agent with autonomous debugging (screenshots, console logs, endpoint probing)
- Snapshot/rollback system for your projects
- Hub UI for launching and managing built apps

**Known rough edges:**
- Some builds may need a second pass through the Manager to get fully working
- Image generation features in built apps require Imagen 4 (the pipeline knows this, but older builds might reference deprecated models)
- The UI is functional but not polished — it's a developer tool, not a design showcase
- Error messages could be friendlier in places

**What we need help with:**
- Try building different kinds of apps and tell us what breaks
- Report any time the pipeline gets stuck or loops without making progress
- Let us know if install fails on your system (OS, Python version, error message)
- UI bugs, confusing flows, unclear error messages — all useful feedback

---

## Project Structure

```
canopy-seed/
├── start.py              # Entry point — boots the server
├── install.bat / .sh     # One-click installers
├── requirements.txt      # Python dependencies
├── pyproject.toml        # Package metadata (pip install . works too)
├── .env.example          # Config template (API keys go in the vault, not here)
│
├── core/                 # The engine — pipeline, agents, tools, API server
├── canopy-ui/            # Browser interface (HTML/CSS/JS)
├── config/               # Settings, model pricing, complexity thresholds
├── tools/                # Shell, file, web fetch utilities
├── skills/               # Extensible skill modules
├── memory/               # SQLite storage + encrypted vault
├── tests/                # Automated test suite
├── docs/                 # Architecture docs, ADRs, marketing materials
│
├── exports/              # Built projects land here
├── logs/                 # Server logs (auto-rotated, API keys redacted)
└── outputs/              # Misc pipeline outputs
```

---

## Troubleshooting

**"Python not found" during install**
Download Python 3.11+ from [python.org](https://python.org). On Windows, make sure to check "Add Python to PATH" during installation. If you already have Python but Windows opens the Microsoft Store, go to Settings > Apps > Advanced app settings > App execution aliases and turn off the Python aliases.

**"ModuleNotFoundError" when running**
The virtual environment might not be activated. Use `START CANOPY.bat` (Windows) or `./start.sh` (Mac/Linux) instead of running `python start.py` directly — the launch scripts activate the venv for you.

**Vault setup screen doesn't appear**
Make sure `.env` exists (the installer creates it). If it's missing, copy `.env.example` to `.env`. The vault screen shows on first launch when no keys have been stored yet.

**"Address already in use" on port 7822**
Another instance of Canopy is already running. Close it first (Ctrl+C in the terminal), then try again.

**Playwright / screenshot errors from the Manager**
Run this inside the virtual environment:
```bash
python -m playwright install chromium
```
The installer tries to do this automatically, but it can fail on some systems. The Manager still works without it — you just won't get screenshots during debugging.

---

## Filing a Bug Report

When something breaks, an issue with this info helps us fix it fast:

1. What you were trying to build (your prompt/idea)
2. Where it failed (during build? in the Manager? on launch?)
3. Any error messages you saw (copy-paste from the terminal or UI)
4. Your setup: Windows/Mac/Linux, Python version (`python --version`)

---

## License

MIT — use it however you want.

---

*Built by Travis Young. Canopy Seed is AI-assisted software that builds AI-assisted software. It's turtles all the way down.*
