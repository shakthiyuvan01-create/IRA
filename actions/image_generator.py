"""image_generator.py — AI Image Generation via HuggingFace Inference API (Stable Diffusion XL).

Generates 4 images per prompt, saves them to Data/generated_images/, and opens them.
Adapted from Jarvis AI Assistant's Backend/ImageGeneration.py.

Requires:
  - huggingface_api_key in config/api_keys.json
  - requests (already in IRA deps)
  - Pillow (already in IRA deps)
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

from config import get_config, is_windows, is_mac, is_linux


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
    api_key = _get_hf_api_key()
    if not api_key:
        return (
            "Image generation requires a HuggingFace API key. "
            "Please add 'huggingface_api_key' to config/api_keys.json, Yuvan."
        )

    out_dir = _ensure_images_dir()
    prompt_slug = "".join(c if c.isalnum() or c in " _-" else "_" for c in prompt)[:40]
    timestamp = __import__("datetime").datetime.now().strftime("%H%M%S")

    print(f"[ImageGen] Generating 4 images for: {prompt}")

    results = []
    for i in range(4):
        seed = randint(0, 1_000_000)
        image_data = _generate_single(prompt, seed, api_key)

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

    count = len(results)
    return (
        f"Generated {count} image{'s' if count > 1 else ''} for '{prompt}', Yuvan. "
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
