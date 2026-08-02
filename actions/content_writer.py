"""content_writer.py — Write content (essays, letters, applications, code, poems, etc.)
to a .txt file and open it in Notepad.

Uses IRA's multi-provider LLM chain (core.llm_client.chat()) instead of an external API.
Adapted from Jarvis AI Assistant's Backend/Automation.py Content() function.
"""

import sys
import subprocess
import platform
from pathlib import Path
from datetime import datetime


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
DATA_DIR = BASE_DIR / "Data"


def _ensure_data_dir() -> Path:
    """Create the Data directory if it doesn't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def _open_in_notepad(filepath: Path) -> None:
    """Open a file in the system's default text editor."""
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.Popen(["notepad.exe", str(filepath)], shell=False)
        elif system == "Darwin":
            subprocess.Popen(["open", "-t", str(filepath)])
        else:
            subprocess.Popen(["xdg-open", str(filepath)])
    except Exception as e:
        print(f"[ContentWriter] Could not open text editor: {e}")


def write_content(topic: str, parameters: dict | None = None) -> str:
    """Generate written content on a topic and save it to a file.

    Uses IRA's core.llm_client.chat() which routes through the
    multi-provider chain (Nara -> Bluesminds -> Gemini).

    Args:
        topic: What to write about (e.g. "an application for sick leave",
               "a poem about nature", "a Python factorial function").

    Returns:
        A spoken summary string for the voice output.
    """
    from core.llm_client import chat

    _ensure_data_dir()

    system_prompt = (
        "You are a professional content writer. Write clear, well-structured content "
        "based on the user's request. Output ONLY the content itself with no "
        "meta-commentary, no introductions, no explanations of what you are writing."
    )

    user_prompt = (
        f"Write the following content. Output ONLY the content with NO extra "
        f"commentary, NO greetings, and NO sign-offs:\n\n{topic}"
    )

    print(f"[ContentWriter] Generating content for: {topic}")

    try:
        content = chat(user_prompt, system=system_prompt, timeout=120)
    except Exception as e:
        print(f"[ContentWriter] LLM error: {e}")
        return f"Failed to generate content, Yuvan. Error: {e}"

    if not content or len(content.strip()) < 10:
        return "I could not generate meaningful content for that topic, Yuvan."

    # Create a safe filename from the topic
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in topic)[:50]
    safe_name = safe_name.strip().replace(" ", "_") or "untitled"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_name}_{timestamp}.txt"
    filepath = DATA_DIR / filename

    filepath.write_text(content, encoding="utf-8")
    print(f"[ContentWriter] Saved: {filepath}")

    _open_in_notepad(filepath)

    # Truncate for spoken response
    preview = content[:150].strip()
    if len(content) > 150:
        preview += "..."

    return (
        f"Content written and saved to {filename}, Yuvan. "
        f"Opening it in Notepad now. Here is a preview: {preview}"
    )


def content_writer(
    parameters: dict | None = None,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Tool entry point called by IRA's tool execution system."""
    params = parameters or {}
    topic = params.get("topic", "").strip()

    if not topic:
        return "Please tell me what you would like me to write, Yuvan."

    if player:
        player.write_log(f"[ContentWriter] Topic: {topic}")
    if speak:
        speak(f"Writing content for '{topic}'. One moment, Yuvan.")

    result = write_content(topic, params)

    # Show on screen
    if player:
        filepath = DATA_DIR / f"{''.join(c if c.isalnum() or c in ' _-' else '_' for c in topic)[:50]}_*.txt"
        player.show_content("CONTENT WRITER", f"Topic: {topic}\n\nSaved to: Data/")

    return result
