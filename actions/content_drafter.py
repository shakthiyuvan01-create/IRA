"""content_drafter.py — AI-powered social media caption drafting + version history.

Adapted from agentic-os-personal-main's server/ai/contentAgent.js.
Uses IRA's core.llm_client.chat() for AI generation.
"""

from data.database import get_db

PLATFORM_NOTES = {
    "instagram": (
        "Instagram: engaging hook, line breaks for readability, "
        "3-8 relevant hashtags at the end."
    ),
    "linkedin": (
        "LinkedIn: professional, insight-led, no hashtag spam (1-3 max), "
        "short paragraphs, a thought-provoking opener."
    ),
    "twitter": (
        "X/Twitter: punchy, under 280 characters, at most 1-2 hashtags, "
        "strong first line."
    ),
}


def _brand_voice():
    """Get brand voice from settings."""
    db = get_db()
    return db.get_setting("brand_voice", "Clear, confident, and concise.")


def generate_caption(topic, platform="general", article_context=None):
    """Generate a social media caption about a topic."""
    from core.llm_client import chat

    platform_note = PLATFORM_NOTES.get(platform, "Write a general social caption.")
    brand = _brand_voice()

    context = ""
    if article_context:
        context = (
            "\n\nGround the post in this source material:\n"
            + str(article_context)[:2000]
        )

    system = (
        "You are a social media copywriter. Brand voice: " + brand + "\n"
        + platform_note + "\n"
        + "Output ONLY the caption. No preamble, no explanations."
    )
    user = "Write a social post about: " + topic + context

    try:
        caption = chat(user, system=system, timeout=60)
        return caption.strip()
    except Exception as e:
        return "Failed to generate caption: " + str(e)


def refine_caption(draft_id, instruction):
    """Conversationally refine an existing caption with version history."""
    from core.llm_client import chat

    db = get_db()
    # Store the refinement instruction
    db.connect().execute(
        "INSERT INTO draft_versions (draft_id, role, content, caption) "
        "VALUES (?, ?, ?, ?)",
        (draft_id, "user", instruction, ""),
    )
    db.connect().commit()

    # Get current caption (from the most recent assistant version)
    row = db.connect().execute(
        "SELECT caption FROM draft_versions WHERE draft_id = ? "
        "AND role = 'assistant' ORDER BY id DESC LIMIT 1",
        (draft_id,),
    ).fetchone()
    current = row["caption"] if row else ""

    brand = _brand_voice()
    system = (
        "You are a social media copy editor. Brand voice: " + brand + "\n"
        "Revise the caption per the user's instruction. "
        "Return ONLY the full revised caption, nothing else."
    )
    user = "Current caption:\n" + (current or "(empty)") + "\n\nRevision: " + instruction

    try:
        revised = chat(user, system=system, timeout=60).strip()
    except Exception as e:
        return {"success": False, "error": str(e)}

    db.connect().execute(
        "INSERT INTO draft_versions (draft_id, role, content, caption) "
        "VALUES (?, ?, ?, ?)",
        (draft_id, "assistant", revised, revised),
    )
    db.connect().commit()

    return {"success": True, "caption": revised}


def generate_image_prompt(topic, caption=""):
    """Generate an image prompt for a social media news card."""
    from core.llm_client import chat

    brand = _brand_voice()
    system = (
        "You write prompts for an AI image model that renders social media news cards. "
        "Describe a clean modern layout with bold headline, supporting subhead, "
        "relevant background imagery, strong contrast. "
        "Brand voice: " + brand + ". "
        "Output ONLY the prompt, no preamble."
    )
    user = (
        "Write an image generation prompt for a social post about: "
        + topic + "\nCaption: " + (caption or topic)
    )
    try:
        prompt = chat(user, system=system, timeout=60)
        return prompt.strip()
    except Exception as e:
        return "Failed to generate image prompt: " + str(e)


def content_drafter(parameters=None, response=None, player=None, session_memory=None, speak=None):
    """Tool entry point — draft or refine a social media caption."""
    params = parameters or {}
    action = params.get("action", "draft").strip().lower()
    topic = params.get("topic", "").strip()
    platform = params.get("platform", "general").strip().lower()
    instruction = params.get("instruction", "").strip()
    draft_id = params.get("draft_id")

    if action == "refine":
        if not instruction or not draft_id:
            return "Please provide both an instruction and a draft_id to refine, Yuvan."
        result = refine_caption(draft_id, instruction)
        if not result.get("success"):
            return "Refinement failed: " + result.get("error", "Unknown error")
        if speak:
            speak("Here is the revised caption, Yuvan.")
        return "Revised caption:\n" + result["caption"]

    if not topic:
        return "Please tell me what topic to write about, Yuvan."

    if player:
        player.write_log("[Draft] " + action + " - " + topic + " (" + platform + ")")
    if speak:
        speak("Drafting a caption for " + platform + ". One moment, Yuvan.")

    caption = generate_caption(topic, platform=platform)

    # Store in DB as a draft version
    db = get_db()
    db.connect().execute(
        "INSERT INTO draft_versions (draft_id, role, content, caption) "
        "VALUES (-1, 'assistant', ?, ?)",
        (caption, caption),
    )
    db.connect().commit()
    new_id = db.connect().execute("SELECT last_insert_rowid()").fetchone()[0]

    # Also create a draft with this ID
    if draft_id is None:
        db.connect().execute(
            "INSERT OR IGNORE INTO draft_versions (draft_id, role, content, caption) "
            "VALUES (-2, 'system', ?, ?)",
            ("Draft: " + topic, caption),
        )
        db.connect().commit()
        draft_id = -2

    return (
        "Caption drafted for " + platform + ", Yuvan.\n\n" + caption
        + "\n\nTell me if you want to refine it."
    )
