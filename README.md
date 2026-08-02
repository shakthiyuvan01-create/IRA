<div align="center">

<img src="IRA.png" alt="IRA Logo" width="160"/>

# 🤖 IRA

**The Ultimate Cross-Platform Personal AI Assistant**

[![Made by Yuvan](https://img.shields.io/badge/Made%20by-Yuvan-gold?style=for-the-badge)](https://github.com)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?style=for-the-badge&logo=python)](https://python.org)
[![Voice](https://img.shields.io/badge/Real--Time-Voice-purple?style=for-the-badge)](https://github.com)
[![Providers](https://img.shields.io/badge/Providers-4--chain%20failover-46E3B7?style=for-the-badge)](https://github.com)
[![Platforms](https://img.shields.io/badge/OS-Windows%20%7C%20macOS%20%7C%20Linux-black?style=for-the-badge)](https://github.com)

> *A real-time voice AI that can hear, see, understand, and control your computer — on any OS.*
> — Made by Yuvan

> 📺 **[Watch the full setup video on YouTube](https://www.youtube.com/watch?v=BhOsnGC_sAA)**

</div>

---

## ✨ What is IRA?

**IRA** — **I**ntelligent **R**esponse **A**ssistant — is a personal AI assistant that bridges
the gap between your operating system, real-time web intelligence, and hardware metrics. Through
natural dialogue it monitors your hardware, prepares your day, and visualizes complex web data
through an adaptive interface. It's not just an assistant — it's an extension of your digital life.

Built with a **PyQt6 desktop UI** for ultra-low-latency voice, a **FastAPI dashboard** for remote
control, and the **JARVIS live-audio loop** for conversation in any language. Zero subscriptions,
total digital autonomy.

---

## 🚀 Quick Start

```bash
git clone https://github.com/shakthiyuvan01-create/IRA.git
cd IRA
pip install -r requirements.txt
playwright install
python main.py
```

### Windows shortcuts

| Shortcut | What it does |
|---|---|
| `Launch IRA.vbs` | Silent launch — no console window |
| `Launch IRA (debug).bat` | Debug launch — shows errors in a window |
| `Install IRA Shortcut.bat` | Creates a desktop shortcut |

> ⚠️ **Installation Note:** some OS-specific dependencies are not bundled in `requirements.txt`.
> If you hit a `ModuleNotFoundError`, install the missing package via `pip install <module_name>`
> for your specific system.

---

## 🔑 Environment

`.env` — any ONE provider key is enough; the chain handles the rest. Keys can also live in
`config/api_keys.json` (merged into the environment at boot).

| Key | Purpose | Get a key |
|---|---|---|
| `GITHUB_TOKEN` | GitHub Models (free GPT-4o) | [github.com/settings/tokens](https://github.com/settings/tokens) |
| `GEMINI_API_KEY` + `GEMINI_MODEL` | Google Gemini (default `gemini-2.5-flash`) — also drives the voice loop | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `OPENAI_API_KEY` + `OPENAI_MODEL` | OpenAI API | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| `OLLAMA_MODEL` + `OLLAMA_URL` | Local Ollama (default `llama3.2`) — no key needed | [ollama.com](https://ollama.com) |
| `AI_PROVIDER_ORDER` | Chain order, first 3 raced in parallel. Default `gemini,openai,ollama` |
| `HEARTBEAT_ENABLED` / `HEARTBEAT_INTERVAL_MINUTES` | Self-improvement heartbeat toggle |

### Supported providers

- **GitHub Models** — free GPT-4o / GPT-4o-mini with a GitHub token
- **Google Gemini** — default `gemini-2.5-flash`, also powers the JARVIS voice loop
- **OpenAI** — full OpenAI API (default `gpt-4o-mini`)
- **Ollama** — fully local models (default `llama3.2`)

---

## 🎙️ Real-Time Voice (JARVIS)

- **Live audio conversation** — streaming mic in, streaming speech out, ultra-low latency
- **Any language** — speaks whatever you speak; dialect is learned silently into your identity
- **Hybrid input** — seamlessly switch between keyboard typing and voice commands
- **Visual awareness** — real-time screen processing and webcam vision (`screen_processor`,
  `hand_gesture_control`)

---

## 🧠 Persistent Memory

- **Memory manager** — long-term project, preference, and personal context (`memory/memory_manager.py`)
- **Session search** — full-text search across past conversations (`memory/session_search.py`)
- **Memory tools** — exposed to the model as tools (`memory_tool`, `session_search_tool`) so it can
  store and recall on its own
- **Persona files** — `persona/SOUL.md`, `IDENTITY.md`, `USER.md`, `TOOLS.md`, `AGENTS.md`,
  `HEARTBEAT.md` define who IRA is and how it remembers

---

## 🛠️ 46 Built-in Actions

Actions auto-import and register as tools the assistant can call:

| Category | Actions |
|---|---|
| **System & Desktop** | `open_app`, `computer_control`, `computer_settings`, `desktop`, `command_runner`, `clipboard_manager`, `task_manager`, `system_monitor` |
| **Web & Search** | `web_search`, `web_scraper`, `browser_control`, `youtube_video`, `rss_collector`, `website_builder` |
| **Files & Docs** | `file_controller`, `file_processor`, `folder_analyzer`, `pdf_tools`, `docx_tools`, `ppt_builder`, `ppt_template_workflow`, `office_builder` |
| **Productivity** | `reminder`, `scheduler`, `daily_briefing`, `expense_tracker`, `content_drafter`, `content_writer`, `meeting_assistant`, `attention_monitor` |
| **Communication** | `send_message`, `gmail_reader`, `mail_reader`, `telegram_control`, `discord_control`, `slack_control` |
| **Dev & Code** | `code_helper`, `dev_agent`, `claude_code_bridge`, `game_updater` |
| **Life & Media** | `weather_report`, `flight_finder`, `image_generator`, `screen_overlay`, `screen_processor`, `hand_gesture_control` |

---

## 🔨 Tool Forge (Ada-SI port)

IRA can **write, test, and install its own tools** at runtime (`forge/`, ~6,500 lines):

- **`tools_engine.py`** — dynamic tool loading, execution, manifest management, interactive skills
- **`tool_creator.py`** — AI code generation pipeline (plan → codegen → validate → preview → install)
- **`tool_verify.py`** — ephemeral venv verification of forged tools
- **`tool_build_stream.py`** — streaming build pipeline (codegen → validate → sandbox → QA → preview → install)
- **`build_pipeline.py`** — shared build/install with pip + runtime verification
- **`build_ui_qa.py`** — interactive skill UI QA (UI validation, API contract tests, automated review)
- **`forge_batch.py`** — multi-tool batch orchestration (2–10 tools in parallel)

The forge uses LiteLLM for AI code generation when configured, or falls back to IRA's own
provider chain.

---

## 🐳 Tool Runtime (sandboxed execution)

- `tool_runtime/` — a **Dockerized Python sandbox** (port **8090**) that runs forged tools in
  isolated venvs
- `runner.py` + FastAPI `server.py` + Python 3.12-slim Dockerfile
- Mounts `custom_tools/` and a persistent tool venv

```bash
docker compose up tool-runtime -d
```

---

## 🖥️ Dashboard & API

- **FastAPI dashboard** on port **8000** (`dashboard/server.py`) — remote control from a browser
- **22 forge routes** (`dashboard/forge_router.py`):
  - Tool plans: create, approve, revise, reject, cancel
  - Tool management: list, delete
  - Pip packages: list, uninstall
  - Batch forge: create, approve, reject, cancel, revise, start build
  - Skill UI: serve files, execute actions, read/write data
- **Static UI** — `dashboard/static/skill-sdk.js` (SDK for custom interactive skill iframes),
  `forge-ui.html` (web chat interface), icons and assets

---

## 🧬 Self-Improvement (bounded & verifiable)

| System | What it does |
|---|---|
| **Heartbeat** | On a timer, reviews recent activity and rewrites its own memory with durable new facts |
| **Self-optimizer** | Runs the eval, tweaks its system prompt for the weakest area, keeps the change ONLY if the score improves, else reverts |
| **Eval harness** | Golden prompts scored automatically; regressions reported (`services/eval_harness.py`, `self_eval.py`) |
| **Experience DB** | Distills reusable strategies from past runs; failures write "avoid" lessons (`services/experience_db.py`) |
| **Curator** | Curates learned knowledge (`services/curator.py`) |

---

## 🏗️ Architecture

```
User ⇄ PyQt6 Desktop UI (JarvisUI) · voice (STT ⇄ Gemini live ⇄ TTS)
          │
Main loop (main.py) ── provider chain ── actions (46) ── memory (persistent)
          │
providers/ ── Gemini → OpenAI → GitHub → Ollama
               (parallel racing on the first 3 + sequential failover + caching)
          │
Self-improvement ── heartbeat · self-optimize · eval harness · experience DB · curator
          │
Tool Forge ── tool_creator · tool_verify · tool_build_stream · forge_batch
          │
Tool Runtime (Docker, :8090) ── sandboxed venv execution of forged tools
          │
Dashboard (FastAPI, :8000) ── 22 forge routes · skill UI · remote control
```

---

## 🗺️ Project Structure

```
IRA/
├── main.py                  # Entry point — JARVIS voice loop, tool routing, briefing
├── ui.py                    # JarvisUI — PyQt6 desktop interface
├── discord_bot.py           # Discord channel
├── or_client.py             # OpenRouter client
├── updater.py               # Self-update logic
├── core/                    # llm_client, stt, tts, installer, user_paths
├── providers/               # 4-provider chain
│   ├── manager.py           # Racing + failover + caching
│   ├── base.py              # Abstract provider
│   ├── github_models.py     # GitHub Models (GPT-4o / 4o-mini)
│   ├── gemini.py            # Google Gemini (drives voice)
│   ├── openai_provider.py   # OpenAI
│   └── ollama.py            # Local Ollama
├── actions/                 # 46 built-in tools (web, files, system, comms, dev…)
├── memory/                  # memory_manager, memory_tool, session_search
├── persona/                 # SOUL · IDENTITY · USER · TOOLS · AGENTS · HEARTBEAT
├── agent/                   # planner, executor, task_queue, error_handler
├── services/                # heartbeat, self_optimize, self_eval, eval_harness, curator…
├── forge/                   # Tool Forge — tool_creator, tool_verify, build_pipeline…
├── tool_runtime/            # Docker sandbox — runner.py, server.py, Dockerfile
├── dashboard/               # FastAPI server + forge_router (22 routes) + static UI
├── smart_home/              # Smart device manager (python-kasa)
├── auth/                    # Auth utilities
├── config/                  # api_keys.json (secrets) — NEVER committed
├── plugins/                 # Drop-in plugin loader
└── data/ · memory/ · staging/   # Runtime data — gitignored
```

---

## 🏠 Smart Home

- `smart_home/` — control smart devices via python-kasa (TP-Link Kasa ecosystem)
- `smart_device_manager.py`, `storage.py`, `service.py`

---

## 🔐 Safety & Privacy

- **Secrets never committed** — `.env`, `config/api_keys.json`, `config/firebase_config.json`,
  `config/smart_home.key`, and certs are all gitignored
- **Local-first memory** — your data, `data/`, and `memory/` stay on your machine
- **Personal & non-commercial** — see the license below

---

## 🐍 Requirements

See `requirements.txt`. Highlights:
- **Voice:** google-genai, sounddevice, pyaudio, edge-tts, miniaudio
- **UI:** PyQt6
- **Vision/control:** playwright, pyautogui, opencv-python, mss, mediapipe
- **Docs:** python-docx, python-pptx, openpyxl, reportlab, pdf (playwright)
- **Web:** requests, beautifulsoup4, duckduckgo-search, feedparser, google-api-python-client
- **Backend:** fastapi, uvicorn, httpx, discord.py, slack_sdk, python-kasa
- **System:** psutil, pyperclip, pygetwindow, send2trash, qrcode[pil], cryptography

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.

---

<div align="center">

**Built with ⚡ by Yuvan** — *a real-time voice AI that can hear, see, understand, and control your computer.*

</div>
