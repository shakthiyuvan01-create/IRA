#!/usr/bin/env python3
"""
 IRA — Headless Room Hub (for the 2013 MacBook Air / any always-on machine)

 A THIN, GUI-FREE voice client that mirrors the exact Gemini Live audio loop
 used by core/main.py (same model, same sample rates, same mic->cloud /
 cloud->speaker wiring) but imports NONE of PyQt6 / torch / whisper / opencv.
 This is what runs 24/7 on the Mac as your room voice brain.

 Mic  : Bluetooth headset  -> Gemini Live (native audio)
 Out  : Bluetooth speaker  <- Gemini Live (native audio)

 WHY miniaudio (not sounddevice): sounddevice needs system PortAudio, which
 is absent on a clean/full macOS install. miniaudio vendors its own backend,
 so it just works with `pip install miniaudio` — no Homebrew, no portaudio.

 Required deps:  google-genai  miniaudio

 Usage (on the Mac, inside the IRA venv):
     python roomhub/mac_voice_hub.py --list-devices     # show audio devices
     python roomhub/mac_voice_hub.py --auto-pick         # headset in / speaker out
     python roomhub/mac_voice_hub.py --input-name "Headset" --output-name "Speaker"

 Keep awake 24/7:
     caffeinate -ims python roomhub/mac_voice_hub.py --auto-pick
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
import traceback
from pathlib import Path

import miniaudio
from google import genai
from google.genai import types

# ── Faithful copy of IRA's live-audio constants (core/main.py lines 129-133) ──
LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-latest"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000

BASE_DIR  = Path(__file__).resolve().parent.parent
API_FILE  = BASE_DIR / "core" / "config" / "api_keys.json"

SYSTEM_PROMPT = (
    "You are IRA, a room voice assistant running 24/7 on a MacBook Air in the user's room. "
    "Respond by voice, in a natural, concise, friendly tone. "
    "The user speaks to you through a Bluetooth headset microphone and hears you through a "
    "Bluetooth speaker. Keep replies short unless asked to explain. "
    "You can answer questions, set reminders, tell the time/weather, and help with tasks. "
    "Do not narrate internal steps."
)


def _get_api_key() -> str:
    with open(API_FILE, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def list_devices() -> str:
    devs = miniaudio.get_devices()
    lines = ["Audio devices:"]
    for d in devs:
        kinds = []
        if d.input:
            kinds.append("IN")
        if d.output:
            kinds.append("OUT")
        lines.append(f"  [{d.id!r}] {d.name} ({', '.join(kinds)})")
    return "\n".join(lines)


def pick_devices() -> tuple[str | None, str | None]:
    """Return (input_device_id, output_device_id) chosen by name heuristics."""
    devs = miniaudio.get_devices()
    in_id = out_id = None
    for d in devs:
        name = (d.name or "").lower()
        if d.input and in_id is None and (
            "headset" in name or "mic" in name or "bluetooth" in name
        ):
            in_id = d.id
        if d.output and out_id is None and (
            "speaker" in name or "bluetooth" in name or "airplay" in name
        ):
            out_id = d.id
    return in_id, out_id


def resolve_device(name_sub: str, want_input: bool):
    name_sub = name_sub.lower()
    for d in miniaudio.get_devices():
        if want_input and not d.input:
            continue
        if not want_input and not d.output:
            continue
        if name_sub in (d.name or "").lower():
            return d.id
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--auto-pick", action="store_true", help="pick headset/speaker by name")
    ap.add_argument("--input-name", type=str, default=None, help="input device name substring")
    ap.add_argument("--output-name", type=str, default=None, help="output device name substring")
    ap.add_argument("--input-id", type=str, default=None, help="raw miniaudio input device id")
    ap.add_argument("--output-id", type=str, default=None, help="raw miniaudio output device id")
    args = ap.parse_args()

    if args.list_devices:
        print(list_devices())
        return

    # Resolve devices
    in_dev = args.input_id
    out_dev = args.output_id
    if in_dev is None and args.input_name:
        in_dev = resolve_device(args.input_name, want_input=True)
    if out_dev is None and args.output_name:
        out_dev = resolve_device(args.output_name, want_input=False)
    if (in_dev is None or out_dev is None) and args.auto_pick:
        a, b = pick_devices()
        in_dev = in_dev or a
        out_dev = out_dev or b

    print(list_devices())
    print(f"[Hub] input_device={in_dev!r}  output_device={out_dev!r}")

    client = genai.Client(api_key=_get_api_key(), http_options={"api_version": "v1beta"})
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        input_audio_transcription={},
        system_instruction=SYSTEM_PROMPT,
    )

    out_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    audio_in_queue: asyncio.Queue = asyncio.Queue()
    speaking = False
    speaking_lock = threading.Lock()
    loop = asyncio.get_event_loop()

    async def send_realtime(session):
        while True:
            msg = await out_queue.get()
            await session.send_realtime_input(audio=msg)

    async def receive_audio(session):
        nonlocal speaking
        async for response in session.receive():
            if response.data:
                audio_in_queue.put_nowait(response.data)
            if response.server_content:
                sc = response.server_content
                if sc.output_transcription and sc.output_transcription.text:
                    print(f"IRA: {sc.output_transcription.text}", end="", flush=True)
                if sc.input_transcription and sc.input_transcription.text:
                    print(f"\nYou: {sc.input_transcription.text}")
                if sc.turn_complete:
                    print()

    async def play_audio():
        nonlocal speaking
        with miniaudio.OutputStream(
            format=miniaudio.SampleFormat.SIGNED16,
            channels=CHANNELS,
            rate=RECEIVE_SAMPLE_RATE,
            device=out_dev or miniaudio.default_output_device_id(),
        ) as stream:
            print("[Hub] Speaker open.")
            while True:
                try:
                    chunk = await asyncio.wait_for(audio_in_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                with speaking_lock:
                    speaking = True
                await asyncio.to_thread(stream.write, chunk)
                with speaking_lock:
                    speaking = False

    def mic_thread():
        nonlocal speaking
        with miniaudio.InputStream(
            format=miniaudio.SampleFormat.SIGNED16,
            channels=CHANNELS,
            rate=SEND_SAMPLE_RATE,
            device=in_dev or miniaudio.default_input_device_id(),
        ) as stream:
            print("[Hub] Mic open. Listening...")
            for _ts, pcm in stream:
                with speaking_lock:
                    is_speaking = speaking
                if not is_speaking:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            out_queue.put({"data": pcm, "mime_type": "audio/pcm"}), loop
                        )
                    except RuntimeError:
                        return

    print("[Hub] Starting 24/7 room voice loop (Ctrl-C to stop)...")
    while True:
        try:
            async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
                print("[Hub] Connected to Gemini Live. Speak into your headset.")
                t = threading.Thread(target=mic_thread, daemon=True)
                t.start()
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(send_realtime(session))
                    tg.create_task(receive_audio(session))
                    tg.create_task(play_audio())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Hub] Session error: {e}")
            traceback.print_exc()
        print("[Hub] Reconnecting in 3s...")
        time.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Hub] Stopped by user.")
