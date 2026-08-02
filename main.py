import asyncio
import re
import threading
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

# ── Load .env for multi-provider support ──────────────────────────────────
from dotenv import load_dotenv
load_dotenv()
# Also merge existing api_keys.json values into os.environ
try:
    _api_config = Path(__file__).resolve().parent / "config" / "api_keys.json"
    if _api_config.exists():
        import os as _os
        _cfg_data = json.loads(_api_config.read_text(encoding="utf-8"))
        for _k, _v in _cfg_data.items():
            if isinstance(_v, str) and _v and not _os.environ.get(_k.upper()):
                _os.environ[_k.upper()] = _v
except Exception:
    pass

# ── Make console output crash-proof ──────────────────────────────────────────
# The app prints emoji (🎤 👂 🔊). On Windows the console/redirect encoding is
# often cp1252, which cannot encode those characters and raises
# UnicodeEncodeError — this previously killed the audio tasks when launched
# from the desktop shortcut. Force UTF-8 (and never crash on an odd character).
import io as _io
import os as _os


def _safe_stream(stream):
    if stream is None:                      # pythonw.exe has no console
        return open(_os.devnull, "w", encoding="utf-8", errors="replace")
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
        return stream
    except Exception:
        try:
            return _io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace")
        except Exception:
            return stream


sys.stdout = _safe_stream(sys.stdout)
sys.stderr = _safe_stream(sys.stderr)
# ─────────────────────────────────────────────────────────────────────────────

import sounddevice as sd
from google import genai
from google.genai import types
from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    append_chat, recent_chat_context,
)
from memory.memory_tool import (
    memory_tool as new_memory_tool,
    session_search_tool,
    get_memory_store,
    get_system_prompt_blocks,
)
from memory.session_search import init_search as init_session_search, index_message as index_chat_message

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import screen_process
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.mail_reader        import read_emails
from actions.folder_analyzer     import analyze_folder
from actions.ppt_builder         import create_presentation
from actions.clipboard_manager   import clipboard, start_clipboard_watch
from actions.command_runner      import run_command
from actions.discord_control     import discord_send, start_discord_bot
from actions.telegram_control    import telegram_send, start_telegram_bot
from actions.scheduler           import scheduler, start_scheduler
from actions.slack_control       import slack_send, start_slack_bot
from actions.hand_gesture_control import HandGestureService
from actions.image_generator    import image_generator
from actions.content_writer     import content_writer
from actions.rss_collector     import rss_collector
from actions.web_scraper       import web_scraper
from actions.content_drafter   import content_drafter
from actions.gmail_reader      import gmail_reader
from actions.expense_tracker   import expense_tracker
from actions.task_manager      import task_manager
from data.database             import init_db

# ── Self-improvement system ──────────────────────────────────────────────
from services.background_tasks import BackgroundTaskManager

# ── Light Brahma AI imports (used at module init) ────────────────────────
from agent.task_queue            import get_queue as get_task_queue, TaskPriority
from actions.daily_briefing      import compile_daily_briefing


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-preview-12-2025"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are IRA, an advanced AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web. Use for ANY question about current facts, events, prices, "
            "or topics — always prefer this over guessing. "
            "Modes: 'search' (default), 'news' (latest headlines on a topic), "
            "'research' (deep comprehensive answer), 'price' (product cost lookup), "
            "'compare' (side-by-side comparison of items)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query or topic"},
                "mode":   {"type": "STRING", "description": "search | news | research | price | compare"},
                "items":  {"type": "ARRAY",  "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | features"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | search | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "generate_images",
        "description": (
            "Generates AI images from a text description using Stable Diffusion. "
            "Use this when the user asks to generate, create, draw, or make an image, "
            "picture, or artwork of something. "
            "Requires a HuggingFace API key configured in api_keys.json."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {"type": "STRING", "description": "Description of the image to generate"},
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "write_content",
        "description": (
            "Writes any type of content (letters, applications, essays, code, poems, "
            "songs, notes, reports, emails) to a .txt file and opens it in Notepad. "
            "Use this when the user asks to write, compose, draft, or create any "
            "written content."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "topic": {"type": "STRING", "description": "Description of what to write (e.g. 'an application for sick leave', 'a poem about nature', 'a Python function to sort numbers')"},
            },
            "required": ["topic"]
        }
    },
    {
        "name": "collect_rss",
        "description": (
            "Parses an RSS/Atom feed and stores articles. "
            "Use this when the user provides an RSS feed URL to collect articles from."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "url": {"type": "STRING", "description": "RSS/Atom feed URL"},
                "tags": {"type": "STRING", "description": "Optional comma-separated topic tags"},
            },
            "required": ["url"]
        }
    },
    {
        "name": "scrape_web",
        "description": (
            "Scrapes a URL for readable content or searches the web. "
            "Use this when the user asks to scrape a website, extract content from a URL, "
            "or search the web for something."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "'scrape' to extract a URL, 'search' for web search"},
                "url": {"type": "STRING", "description": "URL to scrape (for scrape action)"},
                "query": {"type": "STRING", "description": "Search query (for search action)"},
                "save": {"type": "BOOLEAN", "description": "Save scraped content to the article database"},
            },
            "required": []
        }
    },
    {
        "name": "draft_content",
        "description": (
            "Drafts social media captions for Instagram, LinkedIn, or X/Twitter. "
            "Use this when the user asks to write a social media post, caption, "
            "or refine an existing draft."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "'draft' to create new, 'refine' to revise existing"},
                "topic": {"type": "STRING", "description": "Topic for the post"},
                "platform": {"type": "STRING", "description": "Platform: instagram, linkedin, twitter, or general"},
                "instruction": {"type": "STRING", "description": "Refinement instruction (for refine action)"},
                "draft_id": {"type": "NUMBER", "description": "Draft ID to refine"},
            },
            "required": []
        }
    },
    {
        "name": "read_gmail",
        "description": (
            "Reads the user's Gmail inbox. "
            "Use this when the user asks to check their email, read mail, "
            "see the inbox, or asks about new emails. "
            "Requires Google OAuth configuration."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "'list' to read emails, 'status' to check connection"},
                "count": {"type": "NUMBER", "description": "How many recent emails to read (default 5)"},
            },
            "required": []
        }
    },
    {
        "name": "track_expenses",
        "description": (
            "Tracks financial transactions: sync from Gmail or view summary. "
            "Use this when the user asks about expenses, income, spending, "
            "or wants to scan email for transactions."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "'summary' for financial overview, 'list' for recent, 'sync' to scan email"},
                "days": {"type": "NUMBER", "description": "Number of days to look back (default 30)"},
            },
            "required": []
        }
    },
    {
        "name": "task_manager",
        "description": (
            "Manages to-do tasks. Use this when the user asks to add a task, "
            "list tasks, mark a task done, update, or delete a task. "
            "Supports due dates and priority levels."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "'list' (default), 'add', 'done', 'update', or 'delete'"},
                "title": {"type": "STRING", "description": "Task title (required for add/done)"},
                "notes": {"type": "STRING", "description": "Optional notes"},
                "due_date": {"type": "STRING", "description": "Due date YYYY-MM-DD"},
                "priority": {"type": "NUMBER", "description": "0=normal, 1=important, 2=urgent"},
                "task_id": {"type": "NUMBER", "description": "Task ID for done/update/delete"},
                "status": {"type": "STRING", "description": "Filter by status: open, done"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures and analyzes the screen or webcam image. "
            "MUST be called when user asks what is on screen, what you see, "
            "analyze my screen, look at camera, etc. "
            "You have NO visual ability without this tool. "
            "After calling this tool, stay SILENT — the vision module speaks directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "slack_send",
        "description": (
            "Sends a message to Yuvan's Slack via webhook. "
            "Use when Yuvan asks to send/post something to Slack or notify Slack."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "message": {"type": "STRING", "description": "The message text to post to Slack"}
            },
            "required": ["message"]
        }
    },
    {
        "name": "scheduler",
        "description": (
            "Schedules recurring or one-time automations that IRA runs by itself at a set time. "
            "Use when Yuvan says 'every morning', 'every day at', 'each Monday', 'remind me at', "
            "or wants to see/cancel scheduled tasks. "
            "actions: add (prompt + time HH:MM + repeat daily/weekly/once [+ day]), list, cancel (id)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "add | list | cancel"},
                "prompt": {"type": "STRING", "description": "What IRA should do when it fires (for add)"},
                "time":   {"type": "STRING", "description": "Time of day HH:MM (24h) for add"},
                "repeat": {"type": "STRING", "description": "daily | weekly | once (default daily)"},
                "day":    {"type": "STRING", "description": "Weekday for weekly (e.g. Monday)"},
                "id":     {"type": "STRING", "description": "Schedule id to cancel"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "telegram_send",
        "description": (
            "Sends a message to Yuvan's Telegram. "
            "Use when Yuvan asks to send/post something to Telegram or notify Telegram."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "message": {"type": "STRING", "description": "The message text to send to Telegram"}
            },
            "required": ["message"]
        }
    },
    {
        "name": "discord_send",
        "description": (
            "Sends a message to Yuvan's Discord via webhook. "
            "Use when Yuvan asks to send/post something to Discord or notify Discord."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "message": {"type": "STRING", "description": "The message text to post to Discord"}
            },
            "required": ["message"]
        }
    },
    {
        "name": "run_command",
        "description": (
            "Runs a cmd/PowerShell command on Yuvan's computer and returns the output. "
            "Use for terminal commands, scripts, or system queries. "
            "IMPORTANT: if the command is destructive (delete, format, shutdown, registry, "
            "user accounts), ALWAYS ask Yuvan to confirm first, then call again with confirm=true."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING",  "description": "The command line to execute"},
                "confirm": {"type": "BOOLEAN", "description": "Set true ONLY after Yuvan confirmed a destructive command"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "clipboard",
        "description": (
            "Manages Yuvan's clipboard history. Use when Yuvan asks what they copied, "
            "to see clipboard history, or to copy a past item back. "
            "actions: history (list), recall (copy item #index back), save, clear."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",  "description": "history | recall | save | clear"},
                "index":  {"type": "INTEGER", "description": "Which history item to recall (1 = most recent)"},
                "count":  {"type": "INTEGER", "description": "How many history items to list"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "create_presentation",
        "description": (
            "Creates a PowerPoint (.pptx) presentation on a topic and saves it to the Desktop. "
            "Call this when Yuvan asks to make a presentation, slides, deck, or PowerPoint about something."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "topic":  {"type": "STRING",  "description": "What the presentation should be about"},
                "slides": {"type": "INTEGER", "description": "Number of slides (default 6, max 15)"}
            },
            "required": ["topic"]
        }
    },
    {
        "name": "analyze_folder",
        "description": (
            "Analyzes a folder or file at a given path and explains what's there. "
            "Call this when Yuvan gives a folder/file path and asks what's in it, "
            "to analyze a folder, or what a file is. Returns each item's type, size, "
            "and a short description, plus a summary."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "path": {"type": "STRING", "description": "The folder or file path (or shortcut: desktop, downloads, documents, home)"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "read_emails",
        "description": (
            "Reads Yuvan's latest Gmail inbox emails (sender + subject). "
            "Call this whenever Yuvan asks to check email, read mail, see the inbox, "
            "or asks 'any new emails'. Returns a short numbered list."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "count": {"type": "NUMBER", "description": "How many recent emails to read (default 5, max 15)"}
            },
            "required": []
        }
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, any web-based task. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use browser_control or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "shutdown_assistant",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop IRA. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "save_memory",
        "description": (
            "DEPRECATED. Use the 'memory' tool instead. "
            "Save an important personal fact to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity | preferences | projects | relationships | wishes | notes"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name)"},
                "value": {"type": "STRING", "description": "Concise value in English"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "memory",
        "description": (
            "Save durable facts to persistent memory (MEMORY.md / USER.md) that survive "
            "across sessions. Use 'operations' for batch changes (preferred) or "
            "action/content/old_text for single ops.\n\n"
            "TARGETS: 'user' = who the user is (name, role, preferences). "
            "'memory' = your notes (environment, conventions, lessons).\n\n"
            "HOW: make ALL changes in ONE call via an 'operations' array. "
            "The batch applies atomically so you can remove old entries AND add new ones "
            "in a single call even near the char limit.\n\n"
            "WHEN: save proactively on preferences, corrections, personal details, "
            "environment facts, and workflow conventions. "
            "SKIP trivial info, task progress, temp state."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "add | replace | remove (omit when using 'operations')"
                },
                "target": {
                    "type": "STRING",
                    "description": "'memory' for notes, 'user' for user profile"
                },
                "content": {
                    "type": "STRING",
                    "description": "Entry content for add/replace"
                },
                "old_text": {
                    "type": "STRING",
                    "description": "Substring identifying entry for replace/remove"
                },
                "operations": {
                    "type": "ARRAY",
                    "description": "Batch of ops applied atomically. Each: {action, content?, old_text?}",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "action": {"type": "STRING", "description": "add | replace | remove"},
                            "content": {"type": "STRING", "description": "Entry content for add/replace"},
                            "old_text": {"type": "STRING", "description": "Substring for replace/remove"},
                        },
                    },
                },
            },
            "required": ["target"]
        }
    },
    {
        "name": "session_search",
        "description": (
            "Search across ALL past conversations. Use when the user asks "
            "about something discussed in a previous chat. "
            "Call this silently; use results naturally."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "What to look for in past conversations"},
                "limit": {"type": "INTEGER", "description": "Max results (default 10)"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "trigger_heartbeat",
        "description": (
            "Trigger an immediate self-maintenance pass. Reads recent conversation "
            "history and updates long-term memory (MEMORY.md) with any durable facts. "
            "Silent — announces nothing unless new facts were stored."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "trigger_eval",
        "description": (
            "Run the quality evaluation harness immediately. Tests IRA against 8 "
            "golden prompts and reports the overall score. Use when Yuvan asks "
            "'how well are you performing' or 'run a self-check'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "run_self_optimize",
        "description": (
            "Run one self-optimization cycle: measure current quality on golden "
            "prompts, propose a system prompt improvement, test it, and keep it "
            "only if scores improve. Use when Yuvan asks you to improve yourself."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "provider_status",
        "description": (
            "Shows the current status of all AI providers in the multi-provider "
            "chain: which are available, which are in cooldown, and which was "
            "last used. Use when Yuvan asks about provider or AI status."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "audio_list_devices",
        "description": (
            "Lists all available audio input (microphone) and output (speaker/headphone) "
            "devices on this computer. Use when Yuvan asks about audio devices or wants "
            "to switch which device IRA uses for voice input/output."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "audio_set_device",
        "description": (
            "Switch IRA's audio input (microphone) or output (speaker/headphone) device. "
            "Call audio_list_devices first to see device names and IDs. "
            "The setting is saved and persists across restarts."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "type": {
                    "type": "STRING",
                    "description": "'output' for speaker/headphone, 'input' for microphone"
                },
                "device_name": {
                    "type": "STRING",
                    "description": "Partial or full device name (matched case-insensitively against available devices)"
                },
            },
            "required": ["type", "device_name"]
        }
    },
    {
        "name": "set_voice",
        "description": (
            "Switch IRA's voice between Google Gemini (default, low latency) and ElevenLabs (premium quality). "
            "When switching to ElevenLabs, the API key must be configured in config/api_keys.json. "
            "Default is 'gemini'. Use 'elevenlabs' for higher quality voice."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "mode": {
                    "type": "STRING",
                    "description": "'gemini' for Google's built-in voice (Charon) | 'elevenlabs' for ElevenLabs premium voice"
                },
            },
            "required": ["mode"]
        }
    },
    # ── Brahma AI Feature Integrations ─────────────────────────────────────
    {
        "name": "smart_home",
        "description": (
            "Controls smart home devices connected via SmartHomeService. "
            "Use for: turning devices on/off, adjusting fan speed, brightness, "
            "temperature, listing devices, checking home status, or executing "
            "smart home commands. Supports Atomberg, TP-Link Kasa, Philips Hue, "
            "LG ThinQ, Daikin, Tuya, Nest, and SmartThings."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "list_devices | device_count | home_status | execute_command | recent_activity | list_platforms"},
                "command": {"type": "STRING", "description": "Natural language command for execute_action (e.g. 'turn on the fan')"},
                "device_id": {"type": "STRING", "description": "Device ID for device-specific actions"},
                "limit": {"type": "INTEGER", "description": "Activity limit for recent_activity"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "agent_execute",
        "description": (
            "Executes a complex multi-step task using the AI agent system. "
            "The planner breaks down the goal into steps, executes each step, "
            "handles errors, and retries/replans as needed. "
            "Use for any task that requires multiple steps: research a topic and "
            "save results, build something, coordinate multiple actions, "
            "or any complex goal the user describes."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {"type": "STRING", "description": "The task goal or description of what to accomplish"},
                "priority": {"type": "STRING", "description": "low | normal | high (default: normal)"},
            },
            "required": ["goal"]
        }
    },
    {
        "name": "task_queue_status",
        "description": (
            "Shows the status of all tasks in the agent task queue. "
            "Use when the user asks about pending, running, or completed agent tasks."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "claude_code",
        "description": (
            "Bridges to Claude Code CLI for complex development tasks. "
            "Use for: building full websites, coding projects, code editing, "
            "app development, running developer workflows, or any complex "
            "software engineering task that needs the full Claude Code environment."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description": {"type": "STRING", "description": "Detailed description of what to build or code"},
                "workspace_path": {"type": "STRING", "description": "Optional workspace path for the project"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "word_document",
        "description": (
            "Creates and manipulates Microsoft Word (.docx) documents. "
            "Use for: writing letters, reports, documents, resumes/CVs, "
            "contracts, flyers, certificates, invoices, or editing existing "
            "Word files. Supports styles, tables, headers, and formatting."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "create | edit (default: create)"},
                "doc_type": {"type": "STRING", "description": "letter | report | resume | invoice | certificate | flyer | story | generic (default: generic)"},
                "title": {"type": "STRING", "description": "Document title"},
                "content": {"type": "STRING", "description": "Content description or full text"},
                "author": {"type": "STRING", "description": "Author name (optional)"},
                "recipient": {"type": "STRING", "description": "Recipient name (for letters)"},
                "output_path": {"type": "STRING", "description": "Custom output path (optional)"},
            },
            "required": ["title"]
        }
    },
    {
        "name": "create_pdf",
        "description": (
            "Creates a PDF document with formatted content. "
            "Use for generating reports, eBooks, guides, documentation, "
            "or any content that needs to be saved as a PDF file."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING", "description": "PDF document title"},
                "content": {"type": "STRING", "description": "Content to include in the PDF"},
                "author": {"type": "STRING", "description": "Author name (optional)"},
                "output_path": {"type": "STRING", "description": "Custom output path (optional)"},
            },
            "required": ["title", "content"]
        }
    },
    {
        "name": "create_office_presentation",
        "description": (
            "Creates a PowerPoint (.pptx) presentation with AI-generated "
            "content, themes, and slide designs. Use for any presentation, "
            "slide deck, or slideshow request. Supports custom themes, "
            "outlines, and various slide layouts."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING", "description": "Presentation title"},
                "slides": {"type": "INTEGER", "description": "Number of slides (default: 6)"},
                "outline": {"type": "STRING", "description": "Optional outline or topic description"},
                "theme": {"type": "STRING", "description": "Optional theme style"},
                "output_path": {"type": "STRING", "description": "Custom output path (optional)"},
            },
            "required": ["title"]
        }
    },
    {
        "name": "create_spreadsheet",
        "description": (
            "Creates an Excel (.xlsx) spreadsheet. "
            "Use for generating spreadsheets, data tables, budgets, "
            "inventories, financial reports, or any tabular data."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {"type": "STRING", "description": "Spreadsheet title"},
                "headers": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Column headers"},
                "data": {"type": "STRING", "description": "JSON string of row data or content description"},
                "output_path": {"type": "STRING", "description": "Custom output path (optional)"},
            },
            "required": ["title"]
        }
    },
    {
        "name": "website_builder",
        "description": (
            "Builds complete static websites from a text description. "
            "Generates HTML, CSS, and JS files, saves to disk, and opens "
            "a preview. Use for creating landing pages, portfolios, "
            "business sites, documentation sites, or any static website."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description": {"type": "STRING", "description": "Description of the website to build"},
                "output_dir": {"type": "STRING", "description": "Optional output directory"},
                "open": {"type": "BOOLEAN", "description": "Open preview after building (default: true)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "daily_briefing",
        "description": (
            "Compiles and delivers a daily briefing with weather, "
            "system status, workspace summary, and AI-generated suggestions "
            "for the day. Use when the user asks for a morning briefing, "
            "daily update, or 'what's going on today'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "meeting_assistant",
        "description": (
            "Starts or stops the meeting assistant mode. "
            "The meeting assistant watches the screen, captures meeting "
            "content, transcribes audio, and provides real-time summaries "
            "and answers. Use when the user says 'start meeting mode', "
            "'join a meeting', 'take meeting notes', or 'stop meeting mode'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "start | stop | status"},
                "title": {"type": "STRING", "description": "Meeting title (optional)"},
                "context": {"type": "STRING", "description": "Meeting context (optional)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "workspace_store",
        "description": (
            "Manages workspace/project state persistence. "
            "Use for saving and loading project state, session data, "
            "and workspace configurations."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "save | load | list | delete"},
                "key": {"type": "STRING", "description": "Workspace key/name"},
                "data": {"type": "STRING", "description": "JSON data to save (for save action)"},
            },
            "required": ["action"]
        }
    },
]

# --- Plugin system ---


class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self.session        = None
        self.audio_in_queue = None
        self.out_queue      = None
        self._loop          = None
        self._is_speaking   = False
        self._speaking_lock = threading.Lock()
        self._phone_active  = False   # True while phone mic is streaming; pauses PC mic
        self.ui.on_text_command  = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_screen_captured = self._on_screen_captured
        self.ui.on_interrupt       = self.interrupt
        self.ui.on_live_screen     = self._on_live_screen_captured
        try:
            start_clipboard_watch()   # background clipboard-history recorder
        except Exception as _e:
            print(f"[Clipboard] watcher not started: {_e}")
        try:
            start_discord_bot(self._on_text_command)  # optional inbound Discord control
        except Exception as _e:
            print(f"[Discord] bot not started: {_e}")
        try:
            start_telegram_bot(self._on_text_command)  # optional inbound Telegram control
        except Exception as _e:
            print(f"[Telegram] bot not started: {_e}")
        try:
            start_scheduler(self._on_text_command)  # scheduled automations runner
        except Exception as _e:
            print(f"[Scheduler] not started: {_e}")
        try:
            start_slack_bot(self._on_text_command)  # optional inbound Slack control
        except Exception as _e:
            print(f"[Slack] bot not started: {_e}")
        self._turn_done_event: asyncio.Event | None = None
        self._stop_requested = False          # signal for _play_audio to abort
        self._dashboard     = None
        self._briefing_sent = False          # morning briefing fires once per process
        self._sys_monitor   = SystemMonitor()  # persistent cooldown state
        self._bg_tasks      = BackgroundTaskManager(jarvis=self, ui=self.ui)

        # ── New Brahma AI services (lazy — initialised on first use) ──
        self._smart_home       = None
        self._workspace_store  = None
        self._agent_executor   = None
        self._plugin_manager   = None
        self._meeting_assistant = None
        self._task_queue_started = False

        # ── Hand gesture control ──────────────────────────────────────────
        self._gesture_service: HandGestureService | None = None
        self.ui.on_hand_gesture = self._on_toggle_gesture

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _on_screen_captured(self, image_bytes: bytes, mime_type: str):
        """
        Called by the 'See Screen' button AFTER the UI has hidden IRA's own
        window and captured the screen. Sends the image through IRA's existing
        Gemini Live session (instead of creating a separate vision session
        that causes audio conflicts and crashes). Fixes the restart-on-second-use bug.

        IMPORTANT: The prompt tells Gemini NOT to call screen_process tool since
        the image data is already included inline in this message.
        """
        if not self._loop or not self.session:
            print("[IRA] No session — dropping screen capture")
            return
        try:
            import base64
            b64 = base64.b64encode(image_bytes).decode("ascii")
            asyncio.run_coroutine_threadsafe(
                self.session.send_client_content(
                    turns={
                        "parts": [
                            {"inline_data": {"mime_type": mime_type, "data": b64}},
                            {"text": "A screenshot of my screen was just sent attached to this message. Describe what you see briefly. Do NOT call the screen_process or any other tool — the image data is already here in the message with you."},
                        ]
                    },
                    turn_complete=True,
                ),
                self._loop,
            )
            self.ui.write_log(f"SYS: Screen sent to main session ({len(image_bytes):,} bytes)")
        except Exception as e:
            print(f"[IRA] Screen analyze error: {e}")

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def speak(self, text: str):
        if not text or not text.strip():
            return

        # Check if ElevenLabs mode is active
        try:
            cfg_path = BASE_DIR / "persona" / "persona_config.json"
            if cfg_path.exists():
                import json as _json
                _cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                if _cfg.get("tts_mode") == "elevenlabs":
                    self._speak_elevenlabs(text)
                    return
        except Exception:
            pass

        # Default: send to Gemini Live API
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _speak_elevenlabs(self, text: str):
        """Speak text using ElevenLabs TTS, playing on the configured output device."""
        try:
            import requests
            from pathlib import Path
            import json as _json
            import numpy as np

            # Read API key and voice config
            cfg_path = BASE_DIR / "persona" / "persona_config.json"
            api_cfg = _json.loads((BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8"))
            api_key = api_cfg.get("elevenlabs_api_key", "")

            if not api_key:
                # Try persona config as fallback
                if cfg_path.exists():
                    _cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                    api_key = _cfg.get("elevenlabs_api_key", "")

            if not api_key:
                print("[ElevenLabs] No API key configured")
                return

            voice_id = "pNInz6obpgDQGcFmaJgB"  # Default voice (Nicole/Adam depending)

            headers = {
                "xi-api-key": api_key,
                "Content-Type": "application/json",
            }
            payload = {
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            }
            resp = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                json=payload, headers=headers, timeout=30,
            )
            resp.raise_for_status()

            # Play on configured output device
            import miniaudio
            decoded = miniaudio.decode(
                resp.content,
                output_format=miniaudio.SampleFormat.FLOAT32,
                nchannels=1,
            )
            samples = np.array(decoded.samples, dtype=np.float32)
            # Fix: import sounddevice properly
            import sounddevice as _sd
            output_device = self._get_audio_device("audio_output_device", default=None)
            _sd.play(samples, decoded.sample_rate, device=output_device)
            _sd.wait()
        except ImportError:
            print("[ElevenLabs] Missing dependency: pip install miniaudio")
        except Exception as e:
            print(f"[ElevenLabs] Error: {e}")

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Yuvan, {tool_name} encountered an error. {short}")

    def interrupt(self):
        """Interrupt IRA's current speech/response immediately."""
        if not self._loop or not self.session:
            return
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.set()
        # Signal play_audio to stop and drain the audio queue
        self._stop_requested = True
        try:
            while not self.audio_in_queue.empty():
                try:
                    self.audio_in_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        except Exception:
            pass
        try:
            asyncio.run_coroutine_threadsafe(
                self.session.send_client_content(
                    turns={"parts": [{"text": "[INTERRUPT] Stop speaking now and wait silently."}]},
                    turn_complete=True,
                ),
                self._loop,
            )
            self.ui.write_log("SYS: Interrupted")
        except Exception as e:
            print(f"[IRA] Interrupt error: {e}")

    def _on_live_screen_captured(self, image_bytes: bytes, mime_type: str):
        """Handle a live screen capture — sends to main Gemini session."""
        if not self._loop or not self.session:
            print("[IRA] No session — dropping live screen capture")
            return
        try:
            import base64
            b64 = base64.b64encode(image_bytes).decode("ascii")
            asyncio.run_coroutine_threadsafe(
                self.session.send_client_content(
                    turns={
                        "parts": [
                            {"inline_data": {"mime_type": mime_type, "data": b64}},
                            {"text": "Look at this screen capture. Analyze what you see — it could be a math problem, an image/picture, a diagram, text, or anything else. Think step by step and explain your reasoning logically. If it's a math or academic question, solve it step-by-step. If it's an image, describe it. If it's a logical puzzle, work through it. Be thorough but concise. Do NOT call any tools — the image is already here with you."},
                        ]
                    },
                    turn_complete=True,
                ),
                self._loop,
            )
            self.ui.write_log(f"SYS: Live screen sent ({len(image_bytes):,} bytes)")
        except Exception as e:
            print(f"[IRA] Live screen error: {e}")

    def _get_audio_device(self, config_key: str = "audio_output_device", default=None):
        """Read audio device index from persona config, return None for system default."""
        try:
            cfg_path = BASE_DIR / "persona" / "persona_config.json"
            if cfg_path.exists():
                import json as _json
                _cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                device = _cfg.get(config_key)
                if device is not None:
                    if isinstance(device, int):
                        return device
                    if isinstance(device, str) and device.strip():
                        # String device name — resolve to index
                        device_str = device.strip()
                        try:
                            devices = sd.query_devices()
                            for i, d in enumerate(devices):
                                if device_str.lower() in d["name"].lower():
                                    return i
                            print(f"[Audio] Device '{device_str}' not found — using default")
                        except Exception:
                            pass
        except Exception:
            pass
        return default

    def _on_toggle_gesture(self):
        """Toggle hand gesture recognition on/off."""
        if self._gesture_service and self._gesture_service.is_running:
            self._gesture_service.stop()
            self._gesture_service = None
            self.ui.write_log("SYS: Hand gesture service stopped")
        else:
            def on_gesture(description: str):
                self.ui.write_log(f"GESTURE: {description}")

            self._gesture_service = HandGestureService(
                on_gesture=on_gesture,
                camera_id=0,
            )
            self._gesture_service.start()
            self.ui.write_log("SYS: Hand gesture service started — show palm to play/pause")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # memory     = load_memory()  # noqa: F841
        # mem_str    = format_memory_for_prompt(memory)  # noqa: F841
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        parts = [time_ctx]

        # Persona system block (SOUL, IDENTITY, MEMORY from persona files)
        try:
            persona_path = BASE_DIR / "persona"
            persona_files = ["SOUL.md", "IDENTITY.md", "MEMORY.md", "AGENTS.md", "TOOLS.md"]
            persona_blocks = []
            for fname in persona_files:
                fp = persona_path / fname
                if fp.exists():
                    content = fp.read_text(encoding="utf-8").strip()
                    if content:
                        persona_blocks.append(content)
            if persona_blocks:
                parts.append("=== PERSONA ===\n" + "\n\n".join(persona_blocks))
        except Exception:
            pass

        # System overlay (from self-optimization)
        try:
            cfg_path = BASE_DIR / "persona" / "persona_config.json"
            if cfg_path.exists():
                import json as _json
                _cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                overlay = _cfg.get("system_overlay", "")
                if overlay:
                    parts.append("[SYSTEM OVERLAY — self-improvement tuning]\n" + overlay)
        except Exception:
            pass

        # Memory blocks from new MemoryStore (MEMORY.md + USER.md)
        try:
            mem_blocks = get_system_prompt_blocks()
            for block in mem_blocks:
                parts.append(block)
        except Exception:
            pass

        # Legacy memory support (long_term.json — phased out but kept for compat)
        try:
            memory = load_memory()
            legacy_mem = format_memory_for_prompt(memory)
            if legacy_mem:
                parts.append("[LEGACY MEMORY — maintained for backward compatibility]\n" + legacy_mem)
        except Exception:
            pass
        recent = recent_chat_context()
        if recent:
            parts.append(recent)
        parts.append(sys_prompt)

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Charon"
                    )
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        if name == "save_memory":
            # Legacy compatibility — delegate to new memory tool
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                target = "user" if category in ("identity", "preferences", "relationships") else "memory"
                new_memory_tool(action="add", target=target, content=f"{key}: {value}")
                print(f"[Memory] 💾 save_memory → {target}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        if name == "memory":
            result_json = new_memory_tool(**args)
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response=json.loads(result_json)
            )

        if name == "session_search":
            result_json = session_search_tool(
                query=args.get("query", ""),
                limit=args.get("limit", 10),
            )
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response=json.loads(result_json)
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "open_app":
                r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "generate_images":
                r = await loop.run_in_executor(None, lambda: image_generator(parameters=args, response=None, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "write_content":
                r = await loop.run_in_executor(None, lambda: content_writer(parameters=args, response=None, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "collect_rss":
                r = await loop.run_in_executor(None, lambda: rss_collector(parameters=args, response=None, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "scrape_web":
                r = await loop.run_in_executor(None, lambda: web_scraper(parameters=args, response=None, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "draft_content":
                r = await loop.run_in_executor(None, lambda: content_drafter(parameters=args, response=None, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "read_gmail":
                r = await loop.run_in_executor(None, lambda: gmail_reader(parameters=args, response=None, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "track_expenses":
                r = await loop.run_in_executor(None, lambda: expense_tracker(parameters=args, response=None, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "task_manager":
                r = await loop.run_in_executor(None, lambda: task_manager(parameters=args, response=None, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "screen_process":
                threading.Thread(
                    target=screen_process,
                    kwargs={"parameters": args, "response": None,
                            "player": self.ui, "session_memory": None},
                    daemon=True
                ).start()
                result = "Vision module activated. Stay completely silent — vision module will speak directly."

            elif name == "slack_send":
                r = await loop.run_in_executor(None, lambda: slack_send(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "scheduler":
                r = await loop.run_in_executor(None, lambda: scheduler(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "telegram_send":
                r = await loop.run_in_executor(None, lambda: telegram_send(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "discord_send":
                r = await loop.run_in_executor(None, lambda: discord_send(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "run_command":
                r = await loop.run_in_executor(None, lambda: run_command(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "clipboard":
                r = await loop.run_in_executor(None, lambda: clipboard(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "create_presentation":
                r = await loop.run_in_executor(None, lambda: create_presentation(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "analyze_folder":
                r = await loop.run_in_executor(None, lambda: analyze_folder(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "read_emails":
                r = await loop.run_in_executor(None, lambda: read_emails(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."
                # Mirror substantial results to the on-screen content panel
                if r and len(r) > 120:
                    mode  = args.get("mode", "search").upper()
                    query = args.get("query") or ", ".join(args.get("items", []))
                    label = f"{mode} — {query[:38]}" if query else mode
                    self.ui.show_content(label, r)
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "shutdown_assistant":
                self.ui.write_log("SYS: Shutdown requested.")
                self.speak("Goodbye, Yuvan.")
                def _shutdown():
                    import time, os
                    time.sleep(1)
                    os._exit(0)
                threading.Thread(target=_shutdown, daemon=True).start()

            elif name == "trigger_heartbeat":
                r = await self._bg_tasks.trigger_heartbeat()
                if r.get("updated"):
                    result = "Heartbeat complete. Memory has been updated with new facts from our conversation."
                elif r.get("skipped"):
                    result = f"Heartbeat skipped: {r.get('reason', 'not enough activity yet')}"
                else:
                    result = "Heartbeat check complete. Memory is already up to date."

            elif name == "trigger_eval":
                r = await self._bg_tasks.trigger_eval()
                score = r.get("overall", 0)
                result = f"Evaluation complete. Overall quality score: {score}%"
                if r.get("regression"):
                    result += f" ⚠️ Note: {r['regression']}"
                # Show detailed results on screen
                details = r.get("details", [])
                if details:
                    detail_str = "\n".join(
                        f"  {d['area']}: {d['score']}%" for d in details
                    )
                    self.ui.show_content("EVAL RESULTS", detail_str)

            elif name == "run_self_optimize":
                r = await self._bg_tasks.trigger_optimize()
                if r.get("kept"):
                    result = (f"Self-optimization improved my {r.get('target')} score "
                              f"from {r.get('baseline')}% to {r.get('candidate')}%. "
                              f"The improvement has been applied.")
                elif r.get("error"):
                    result = f"Optimization failed: {r['error']}"
                else:
                    result = (f"I attempted to improve my {r.get('target')} area "
                              f"but the score didn't increase enough to keep the change. "
                              f"Reverted to previous prompt.")

            elif name == "provider_status":
                try:
                    from providers import AI
                    st = AI.status()
                    lines = ["Multi-Provider Chain Status:"]
                    for name, avail in st.get("available", {}).items():
                        status = "✅" if avail else "❌"
                        lines.append(f"  {status} {name}")
                    last = st.get("last_used")
                    if last:
                        lines.append(f"\nLast used: {last}")
                    open_circuits = st.get("circuit_open", {})
                    if open_circuits:
                        lines.append("\nProviders in cooldown:")
                        for p, secs in open_circuits.items():
                            lines.append(f"  ⏳ {p} ({secs}s remaining)")
                    result = "\n".join(lines)
                except Exception as e:
                    result = f"Provider system: {e}"

            elif name == "audio_list_devices":
                try:
                    devices = sd.query_devices()
                    lines = ["Available Audio Devices:"]
                    for i, d in enumerate(devices):
                        direction = []
                        if d["max_input_channels"] > 0:
                            direction.append("🎤 IN")
                        if d["max_output_channels"] > 0:
                            direction.append("🔊 OUT")
                        default = ""
                        if d.get("default_samplerate"):
                            try:
                                default_info = sd.query_devices(kind="output")
                                if default_info and default_info["name"] == d["name"]:
                                    default = " ⬅️ (default output)"
                            except Exception:
                                pass
                            try:
                                default_info = sd.query_devices(kind="input")
                                if default_info and default_info["name"] == d["name"]:
                                    default = " ⬅️ (default input)"
                            except Exception:
                                pass
                        lines.append(f"  [{i}] {d['name']} ({', '.join(direction)}){default}")
                    # Show currently configured devices
                    cfg_path = BASE_DIR / "persona" / "persona_config.json"
                    try:
                        import json as _json
                        _cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                        out_dev = _cfg.get("audio_output_device", "default")
                        in_dev = _cfg.get("audio_input_device", "default")
                        lines.append(f"\nConfigured: output={out_dev}, input={in_dev}")
                    except Exception:
                        pass
                    result = "\n".join(lines)
                except Exception as e:
                    result = f"Audio device listing failed: {e}"

            elif name == "audio_set_device":
                try:
                    dev_type = args.get("type", "").strip().lower()
                    dev_name = args.get("device_name", "").strip()
                    if dev_type not in ("input", "output"):
                        result = "Invalid type. Use 'input' or 'output'."
                    elif not dev_name:
                        result = "device_name is required."
                    else:
                        # Find matching device
                        devices = sd.query_devices()
                        found_idx = None
                        for i, d in enumerate(devices):
                            if dev_name.lower() in d["name"].lower():
                                if dev_type == "output" and d["max_output_channels"] > 0:
                                    found_idx = i
                                    break
                                elif dev_type == "input" and d["max_input_channels"] > 0:
                                    found_idx = i
                                    break
                        if found_idx is None:
                            result = f"No {dev_type} device found matching '{dev_name}'."
                        else:
                            config_key = "audio_output_device" if dev_type == "output" else "audio_input_device"
                            cfg_path = BASE_DIR / "persona" / "persona_config.json"
                            import json as _json
                            if cfg_path.exists():
                                _cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                            else:
                                _cfg = {}
                            _cfg[config_key] = found_idx
                            cfg_path.parent.mkdir(parents=True, exist_ok=True)
                            cfg_path.write_text(_json.dumps(_cfg, indent=2) + "\n", encoding="utf-8")
                            dev_name_found = devices[found_idx]["name"]
                            result = f"Audio {dev_type} set to [{found_idx}] {dev_name_found}. Restart IRA for the change to take effect."
                except Exception as e:
                    result = f"Audio device setting failed: {e}"

            elif name == "set_voice":
                try:
                    mode = args.get("mode", "").strip().lower()
                    if mode not in ("gemini", "elevenlabs"):
                        result = "Invalid voice mode. Use 'gemini' or 'elevenlabs'."
                    else:
                        cfg_path = BASE_DIR / "persona" / "persona_config.json"
                        import json as _json
                        if cfg_path.exists():
                            _cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                        else:
                            _cfg = {}
                        _cfg["tts_mode"] = mode

                        # Also save ElevenLabs key if provided
                        api_key = args.get("api_key", "")
                        if api_key:
                            try:
                                api_cfg_path = BASE_DIR / "config" / "api_keys.json"
                                api_cfg = _json.loads(api_cfg_path.read_text(encoding="utf-8"))
                                api_cfg["elevenlabs_api_key"] = api_key
                                api_cfg_path.write_text(_json.dumps(api_cfg, indent=2) + "\n", encoding="utf-8")
                            except Exception:
                                pass

                        cfg_path.parent.mkdir(parents=True, exist_ok=True)
                        cfg_path.write_text(_json.dumps(_cfg, indent=2) + "\n", encoding="utf-8")

                        if mode == "elevenlabs":
                            result = "Voice switched to ElevenLabs premium. Your next response will use ElevenLabs voice."
                        else:
                            result = "Voice switched to Google Gemini (Charon). Default low-latency voice."
                except Exception as e:
                    result = f"Voice switch failed: {e}"

            # ── Brahma AI Feature Integrations ─────────────────────────────────────
            elif name == "smart_home":
                action = args.get("action", "list_devices")
                try:
                    from smart_home import SmartHomeService
                    if self._smart_home is None:
                        self._smart_home = SmartHomeService()
                    smart_home = self._smart_home
                    if action == "list_devices":
                        devices = smart_home.list_devices(args.get("search", ""), args.get("room", ""))
                        result = f"Smart Home Devices ({len(devices)}):\n" + "\n".join(
                            f"  {d.get('name')} ({d.get('room')}) - {'✅ On' if d.get('is_on') else '❌ Off'}"
                            for d in devices
                        ) if devices else "No smart home devices connected."
                    elif action == "device_count":
                        count = smart_home.device_count()
                        result = f"You have {count} smart home device(s) connected."
                    elif action == "home_status":
                        status = smart_home.home_status()
                        result = f"Smart Home: {'Connected' if status['connected'] else 'Disconnected'} — {status['device_count']} device(s)"
                    elif action == "execute_command":
                        r = smart_home.execute_command(args.get("command", ""))
                        result = f"Smart Home: {r.get('detail', 'Done.')}"
                    elif action == "list_platforms":
                        platforms = smart_home.list_platforms()
                        result = "Available Smart Home Platforms:\n" + "\n".join(
                            f"  {'✅' if p.available else '⏳'} {p.name} ({p.key})"
                            for p in platforms
                        )
                    elif action == "recent_activity":
                        activities = smart_home.recent_activity(args.get("limit", 10))
                        result = "Recent Smart Home Activity:\n" + "\n".join(
                            f"  {a.get('title')}: {a.get('detail')}" for a in activities
                        ) if activities else "No recent activity."
                    else:
                        result = f"Unknown smart_home action: {action}"
                except Exception as e:
                    result = f"Smart Home error: {e}"

            elif name == "agent_execute":
                goal = args.get("goal", "")
                priority_str = args.get("priority", "normal")
                from agent.task_queue import get_queue as _get_queue, TaskPriority as _TP
                priority_map = {"low": _TP.LOW, "normal": _TP.NORMAL, "high": _TP.HIGH}
                priority = priority_map.get(priority_str, _TP.NORMAL)
                if not self._task_queue_started:
                    _get_queue().start()
                    self._task_queue_started = True
                task_id = _get_queue().submit(goal, priority=priority, speak=self.speak, on_complete=None)
                result = f"Task queued with ID: {task_id}. I'll work on: {goal[:100]}"

            elif name == "task_queue_status":
                from agent.task_queue import get_queue as _get_queue
                tasks = _get_queue().get_all_statuses()
                if not tasks:
                    result = "No tasks in the queue."
                else:
                    lines = ["Task Queue Status:"]
                    for t in tasks:
                        lines.append(f"  [{t['status']}] {t['task_id']}: {t['goal']}")
                    result = "\n".join(lines)

            elif name == "claude_code":
                import sys as _sys
                _sys.modules.pop('actions.claude_code_bridge', None)
                from actions.claude_code_bridge import run_developer_mode_request as _run_cc
                description = args.get("description", "")
                workspace = args.get("workspace_path", str(BASE_DIR))
                r = await loop.run_in_executor(
                    None, lambda: _run_cc(
                        {"description": description, "workspace_path": workspace}, speak=self.speak
                    )
                )
                result = r or "Claude Code task completed."

            elif name == "word_document":
                from actions.docx_tools import word_document as _wd
                r = await loop.run_in_executor(
                    None, lambda: _wd(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Document created."

            elif name == "create_pdf":
                from actions.pdf_tools import create_pdf as _cp
                r = await loop.run_in_executor(
                    None, lambda: _cp(parameters=args, player=self.ui)
                )
                result = r or "PDF created."

            elif name == "create_office_presentation":
                from actions.office_builder import create_presentation as _cop
                r = await loop.run_in_executor(
                    None, lambda: _cop(parameters=args, player=self.ui)
                )
                result = r or "Presentation created."

            elif name == "create_spreadsheet":
                from actions.office_builder import create_spreadsheet as _cs
                r = await loop.run_in_executor(
                    None, lambda: _cs(parameters=args, player=self.ui)
                )
                result = r or "Spreadsheet created."

            elif name == "website_builder":
                from actions.website_builder import website_builder as _wb
                r = await loop.run_in_executor(
                    None, lambda: _wb(parameters=args, player=self.ui)
                )
                result = r or "Website built."

            elif name == "daily_briefing":
                try:
                    r = compile_daily_briefing({})
                    result = r or "Good morning! Here's your briefing."
                except Exception as e:
                    result = f"Briefing error: {e}"

            elif name == "meeting_assistant":
                action = args.get("action", "status")
                if action == "start":
                    if not self._meeting_assistant:
                        from actions.meeting_assistant import MeetingAssistant as _MA
                        self._meeting_assistant = _MA(
                            on_update=lambda d: self.ui.write_log(f"MEETING: {d.get('summary', '')}"),
                            on_state=lambda s: self.ui.set_state(s),
                        )
                    self._meeting_assistant.start(
                        title=args.get("title", "Meeting"),
                        context=args.get("context", ""),
                    )
                    result = "Meeting assistant started. I'm watching and listening."
                elif action == "stop":
                    if hasattr(self, '_meeting_assistant') and self._meeting_assistant:
                        self._meeting_assistant.stop()
                        result = "Meeting assistant stopped."
                    else:
                        result = "Meeting assistant is not running."
                else:
                    running = hasattr(self, '_meeting_assistant') and self._meeting_assistant and self._meeting_assistant._running if hasattr(self._meeting_assistant, '_running') else False
                    result = f"Meeting assistant is {'running' if running else 'stopped'}."

            elif name == "workspace_store":
                action = args.get("action", "list")
                try:
                    from services.workspace_store import WorkspaceStore as _WS
                    if self._workspace_store is None:
                        self._workspace_store = _WS()
                    store = self._workspace_store
                    if action == "save":
                        key = args.get("key", "default")
                        data = args.get("data", "{}")
                        import json as _json
                        if isinstance(data, str):
                            data = _json.loads(data)
                        store.save(key, data)
                        result = f"Workspace '{key}' saved."
                    elif action == "load":
                        key = args.get("key", "default")
                        data = store.load(key)
                        result = f"Workspace '{key}': {data}" if data else f"Workspace '{key}' not found."
                    elif action == "list":
                        keys = store.list_keys()
                        result = "Saved workspaces: " + (", ".join(keys) if keys else "none")
                    elif action == "delete":
                        key = args.get("key", "")
                        store.delete(key)
                        result = f"Workspace '{key}' deleted."
                    else:
                        result = f"Unknown workspace action: {action}"
                except Exception as e:
                    result = f"Workspace store error: {e}"

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if not jarvis_speaking and not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )

        try:
            input_device = self._get_audio_device("audio_input_device", default=None)
            if input_device is not None:
                print(f"[JARVIS] 🎤 Input device: {input_device}")

            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=callback,
                device=input_device,
            ):
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        if self._turn_done_event and self._turn_done_event.is_set():
                            self._turn_done_event.clear()
                        self.audio_in_queue.put_nowait(response.data)

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt:
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                append_chat("Yuvan", full_in)
                                try:
                                    from memory.session_search import index_message as index_msg
                                    index_msg(datetime.now().isoformat(), "user", full_in)
                                except Exception:
                                    pass
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"IRA: {full_out}")
                                append_chat("IRA", full_out)
                                try:
                                    from memory.session_search import index_message as index_msg
                                    index_msg(datetime.now().isoformat(), "assistant", full_out)
                                except Exception:
                                    pass
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        # Use configured audio output device if specified
        output_device = self._get_audio_device("audio_output_device", default=None)
        if output_device is not None:
            print(f"[JARVIS] 🔊 Output device: {output_device}")

        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
            device=output_device,
        )
        stream.start()

        try:
            while True:
                # Check if stop was requested — immediately abort playback
                if self._stop_requested:
                    self._stop_requested = False
                    self.set_speaking(False)
                    # Stop current audio instantly
                    stream.stop()
                    # Drain remaining chunks
                    while not self.audio_in_queue.empty():
                        try:
                            self.audio_in_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    # Restart stream for next response
                    stream.start()
                    if self._turn_done_event:
                        self._turn_done_event.clear()
                    continue

                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue
                self.set_speaking(True)
                await asyncio.to_thread(stream.write, chunk)
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing for instant perceived response:
          Phase 1 — immediate greeting (no tools, no fetch) → Jarvis speaks in <2s
          Phase 2 — news fetched in background, injected after greeting finishes
        """
        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── memory ───────────────────────────────────────────────────────────
        memory   = load_memory()
        identity = memory.get("identity", {})

        # Also try new MemoryStore for identity data
        try:
            from memory.memory_tool import get_memory_store
            _new_store = get_memory_store()
            _user_id = _new_store.get_user_identity()
            if _user_id.get("name") and not identity:
                identity["name"] = {"value": _user_id["name"]}
            if _user_id.get("language") and not identity.get("language"):
                identity["language"] = {"value": _user_id["language"]}
            if _user_id.get("city") and not identity.get("city"):
                identity["city"] = {"value": _user_id["city"]}
        except Exception:
            pass

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")

        from datetime import datetime
        time_str = datetime.now().strftime("%H:%M")

        # ── Phase 1: instant greeting — zero data needed ──────────────────────
        p1_lines = [
            "[STARTUP_GREETING] Greet the user immediately. Keep it to 1-2 short sentences.",
            f"Current time: {time_str}.",
            "- Say hello and mention the time naturally.",
            "- Say you are checking system status and will share it in a moment.",
            "- Do NOT call any tools. Do NOT say [STARTUP_GREETING].",
            "- Respond in "
            + (f"language: {lang}." if lang else "the user's language (default: English)."),
        ]
        if name:
            p1_lines.append(f"- Address the user as {name}.")

        await self.session.send_client_content(
            turns={"parts": [{"text": '\n'.join(p1_lines)}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fetch system stats in background, deliver after greeting ───
        async def _guarded_system():
            try:
                await self._briefing_system_phase(lang)
            except Exception as e:
                print(f"[Briefing] System phase error: {e}")
                self.ui.write_log(f"SYS: Briefing system phase failed: {e}")
        asyncio.create_task(_guarded_system())

    async def _briefing_system_phase(self, lang: str) -> None:
        """
        Gathers system stats: CPU, GPU, battery, Kotha Gudem temperature,
        primary drive storage. Then injects a short summary into the Live session.
        Waits enough time for the phase-1 greeting to finish playing first.
        """
        import psutil
        import time as _time

        fetch_start = asyncio.get_event_loop().time()

        # Collect system data
        system_data = {}

        # CPU
        system_data["cpu"] = psutil.cpu_percent(interval=0.5)

        # GPU — try to get GPU load
        gpu_val = -1.0
        try:
            import subprocess as _sp
            result = _sp.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0:
                gpu_val = float(result.stdout.strip())
        except Exception:
            try:
                gpu_val = _metrics.snapshot().get("gpu", -1.0)
            except Exception:
                gpu_val = -1.0
        system_data["gpu"] = gpu_val

        # Battery
        battery = psutil.sensors_battery()
        if battery:
            system_data["battery"] = battery.percent
            system_data["battery_charging"] = battery.power_plugged
        else:
            system_data["battery"] = None
            system_data["battery_charging"] = None

        # Primary drive storage (C:)
        try:
            disk = psutil.disk_usage("C:\\")
            system_data["disk_total"] = disk.total / (1024**3)   # GB
            system_data["disk_used"] = disk.used / (1024**3)
            system_data["disk_free"] = disk.free / (1024**3)
            system_data["disk_percent"] = disk.percent
        except Exception:
            system_data["disk_total"] = system_data["disk_used"] = None

        # Temperature for Kotha Gudem — use weather API
        weather_temp = None
        try:
            from actions.weather_report import weather_action
            weather_result = await asyncio.to_thread(
                lambda: weather_action(parameters={"city": "Kotha Gudem"}, player=None)
            )
            if weather_result:
                weather_temp = weather_result
        except Exception as e:
            print(f"[Briefing] Weather fetch: {e}")

        # Show data on screen
        lines = []
        lines.append(f"CPU: {system_data['cpu']:.0f}%")
        if system_data["gpu"] >= 0:
            lines.append(f"GPU: {system_data['gpu']:.0f}%")
        else:
            lines.append(f"GPU: N/A")
        if system_data["battery"] is not None:
            charge = "Charging" if system_data["battery_charging"] else "On Battery"
            lines.append(f"Battery: {system_data['battery']:.0f}% ({charge})")
        else:
            lines.append(f"Battery: N/A")
        if system_data["disk_total"] is not None:
            lines.append(f"Storage (C:): {system_data['disk_used']:.1f}GB / {system_data['disk_total']:.0f}GB ({system_data['disk_percent']:.0f}% used)")
        if weather_temp:
            lines.append(f"Kotha Gudem: {weather_temp}")
        full_report = "\n".join(lines)
        self.ui.show_content("SYSTEM STATUS", full_report)

        # Ensure the phase-1 greeting (≈ 3 s of speech) has finished before we speak again
        elapsed = asyncio.get_event_loop().time() - fetch_start
        wait_more = max(0.0, 3.5 - elapsed)
        if wait_more > 0:
            await asyncio.sleep(wait_more)

        if not self.session:
            return

        # Build the spoken briefing prompt
        p2_lines = [
            "[BRIEFING_SYSTEM] System status is displayed on screen.",
            "Data:",
            f"CPU: {system_data['cpu']:.0f}% used.",
            "GPU: {}% used.".format('N/A' if system_data['gpu'] < 0 else f"{system_data['gpu']:.0f}"),
        ]
        if system_data["battery"] is not None:
            charge = "charging" if system_data["battery_charging"] else "on battery"
            p2_lines.append(f"Battery: {system_data['battery']:.0f}% ({charge}).")
        if system_data["disk_total"] is not None:
            p2_lines.append(f"Primary drive: {system_data['disk_used']:.1f} of {system_data['disk_total']:.0f} gigabytes used.")
        if weather_temp:
            p2_lines.append(f"Kotha Gudem temperature: {weather_temp}.")

        p2_lines += [
            "",
            "Voice rules:",
            "- Speak the stats naturally — one short sentence each.",
            "- Mention CPU, GPU, battery, storage, and weather in that order.",
            "- Keep it brief — max 3 sentences total.",
            "- Say the full details are on screen.",
            "- Ask if they need anything.",
            "- Do NOT say [BRIEFING_SYSTEM].",
            "- Respond in "
            + (f"language: {lang}." if lang else "the user's language."),
        ]

        await self.session.send_client_content(
            turns={"parts": [{"text": '\n'.join(p2_lines)}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing system status sent.")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if alert and self.session:
                try:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": alert}]},
                        turn_complete=True,
                    )
                except Exception as e:
                    print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()

        client = genai.Client(
            api_key=_get_api_key(),
            http_options={"api_version": "v1beta"}
        )

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                config = self._build_config()

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    print("[JARVIS] Connected.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: IRA online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Background self-improvement tasks
                    tg.create_task(self._bg_tasks.start())

                    # Morning briefing — fires once per process launch
                    if not self._briefing_sent:
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except Exception as e:
                print(f"[JARVIS] Error: {e}")
                traceback.print_exc()
            finally:
                self.session = None

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            print("[JARVIS] Reconnecting in 3s...")
            await asyncio.sleep(3)

def main():
    # Seed persona files (SOUL.md, MEMORY.md, etc.)
    try:
        from memory.persona import ensure_persona_layout
        ensure_persona_layout()
    except Exception:
        pass

    # Initialize memory engine (MEMORY.md / USER.md with migration from long_term.json)
    try:
        from memory.memory_tool import get_memory_store
        store = get_memory_store()
        print(f"[Memory] MemoryStore loaded: {store.has_entries('memory')} memory entries, {store.has_entries('user')} user entries")
    except Exception as e:
        print(f"[Memory] MemoryStore init skipped: {e}")

    # Initialize FTS5 session search index
    try:
        from memory.session_search import init_search as init_fts
        count = init_fts()
        print(f"[Search] Session search index built: {count} messages")
    except Exception as e:
        print(f"[Search] Session search init skipped: {e}")

    # Initialize the content database (tasks, articles, expenses, mail)
    try:
        from data.database import init_db
        init_db()
        print("[DB] Content database initialised.")
    except Exception as e:
        print(f"[DB] Init skipped: {e}")

    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()