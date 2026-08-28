"""image_generator.py — AI Image Generation (HuggingFace SDXL, with Gemini fallback).

Generates 4 images per prompt, saves them to Data/generated_images/, and opens them.

Backends (auto-selected):
  - HuggingFace Inference API (stable-diffusion-xl-base-1.0) when
    huggingface_api_key is configured in api_keys.json
  - Gemini image model (gemini-2.5-flash-image) via the existing gemini_api_key
    when no HuggingFace key is present — no extra credentials needed.
"""

import asyncio
import io
import os
import sys
import subprocess
import platform
from pathlib import Path
from random import randint

import requests
from PIL import Image

from core.config import get_config, is_windows, is_mac, is_linux


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
IMAGES_DIR = BASE_DIR / "Data" / "generated_images"

# HuggingFace Inference API endpoint for Stable Diffusion XL
API_URL = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"


def _get_hf_api_key() -> str | None:
    """Read HuggingFace API key from config."""
    cfg = get_config()
    return cfg.get("huggingface_api_key") or None


def _get_gemini_api_key() -> str | None:
    """Read Gemini API key from config (used as the image-gen fallback)."""
    cfg = get_config()
    return cfg.get("gemini_api_key") or None


# Gemini image generation model (google-genai, response_modalities=["IMAGE"])
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"


def _generate_single_gemini(prompt: str, seed: int) -> bytes | None:
    """Generate one image via the Gemini image model (no extra key needed)."""
    api_key = _get_gemini_api_key()
    if not api_key:
        return None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_IMAGE_MODEL,
            contents=(
                f"{prompt}, quality=4K, sharpness=maximum, "
                f"Ultra High details, high resolution, seed={seed}"
            ),
            config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
        )
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                return part.inline_data.data
        print(f"[ImageGen] Gemini returned no inline image (seed={seed})")
    except Exception as e:
        print(f"[ImageGen] Gemini failed (seed={seed}): {e}")
    return None


def _generate_single_pollinations(prompt: str, seed: int) -> bytes | None:
    """Free no-key fallback: Pollinations.ai image endpoint."""
    try:
        from urllib.parse import quote
        import requests

        url = (
            "https://image.pollinations.ai/prompt/"
            f"{quote(f'{prompt}, quality=4K, sharpness=maximum, seed={seed}')}"
            f"?width=512&height=512&seed={seed}&nologo=true"
        )
        resp = requests.get(url, timeout=90)
        resp.raise_for_status()
        return resp.content or None
    except Exception as e:
        print(f"[ImageGen] Pollinations failed (seed={seed}): {e}")
        return None


def _ensure_images_dir() -> Path:
    """Create the output directory if it doesn't exist."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return IMAGES_DIR


def _generate_single(prompt: str, seed: int, api_key: str) -> bytes | None:
    """Generate one image via HuggingFace Inference API."""
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "inputs": (
            f"{prompt}, quality=4K, sharpness=maximum, "
            f"Ultra High details, high resolution, seed={seed}"
        )
    }

    try:
        response = requests.post(API_URL, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        return response.content
    except requests.exceptions.RequestException as e:
        print(f"[ImageGen] Failed (seed={seed}): {e}")
        return None


def _open_image(path: Path) -> None:
    """Open an image file with the default system viewer."""
    try:
        if is_windows():
            os.startfile(str(path))
        elif is_mac():
            subprocess.Popen(["open", str(path)])
        elif is_linux():
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        print(f"[ImageGen] Could not open image: {e}")


def generate_images(prompt: str, parameters: dict | None = None) -> str:
    """Generate 4 AI images from a text prompt.

    Args:
        prompt: The text description of the image to generate.

    Returns:
        A spoken summary string for the voice output.
    """
    hf_key    = _get_hf_api_key()
    gemini_key = _get_gemini_api_key()
    if not hf_key and not gemini_key:
        print("[ImageGen] No API keys found — using the free Pollinations fallback")
    print(f"[ImageGen] Generating 4 images for: {prompt}")

    out_dir = _ensure_images_dir()
    prompt_slug = " ".join(prompt.split()).replace(" ", "_")[:40].strip("_")
    timestamp = __import__("datetime").datetime.now().strftime("%H%M%S")

    # Per-image engine chain: HuggingFace (key) → Gemini (key) → Pollinations
    # (free, no key). Engines that fail are dropped for the remaining images.
    engines: list[tuple[str, object]] = []
    if hf_key:
        engines.append(("huggingface", lambda s: _generate_single(prompt, s, hf_key)))
    if gemini_key:
        engines.append(("gemini", lambda s: _generate_single_gemini(prompt, s)))
    engines.append(("pollinations", lambda s: _generate_single_pollinations(prompt, s)))

    dead: set[str] = set()
    used: set[str] = set()

    results = []
    for i in range(4):
        seed = randint(0, 1_000_000)
        image_data = None
        for name, fn in engines:
            if name in dead:
                continue
            image_data = fn(seed)
            if image_data:
                used.add(name)
                break
            dead.add(name)
            print(f"[ImageGen] engine '{name}' unavailable — trying next")

        if image_data:
            try:
                filename = f"{prompt_slug}_{timestamp}_{i+1}.jpg"
                filepath = out_dir / filename
                filepath.write_bytes(image_data)

                img = Image.open(io.BytesIO(image_data))
                img.verify()

                print(f"[ImageGen] Saved: {filepath}")
                results.append(filepath)
            except Exception as e:
                print(f"[ImageGen] Error saving image {i+1}: {e}")
        else:
            print(f"[ImageGen] Failed to generate image {i+1}")

    if not results:
        return (
            "Image generation failed, Yuvan. "
            "Please check your HuggingFace API key and try again."
        )

    _open_image(results[0])

    engine_desc = ", ".join(sorted(used)) or "pollinations"
    count = len(results)
    return (
        f"Generated {count} image{'s' if count > 1 else ''} for '{prompt}' "
        f"(via {engine_desc}), Yuvan. "
        f"Saved to the Data folder. Opening the first one now."
    )


def image_generator(
    parameters: dict | None = None,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Tool entry point called by IRA's tool execution system."""
    params = parameters or {}
    prompt = params.get("prompt", "").strip()

    if not prompt:
        return "Please provide a description of the image you would like me to generate, Yuvan."

    if player:
        player.write_log(f"[ImageGen] Generating: {prompt}")
    if speak:
        speak(f"Generating images. One moment, Yuvan.")

    result = generate_images(prompt, params)

    images_list = list(IMAGES_DIR.glob("*.jpg"))
    if images_list and player:
        latest = max(images_list, key=lambda p: p.stat().st_mtime)
        parent_text = (
            f"Prompt: {prompt}\n"
            f"Location: {IMAGES_DIR}\n"
            f"Latest: {latest.name}\n"
        )
        player.show_content("IMAGE GENERATION", parent_text)

    return result
