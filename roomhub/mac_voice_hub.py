#!/usr/bin/env python3
"""
 IRA — Headless Room Hub (for the 2013 MacBook Air / any always-on machine)

 A THIN, GUI-FREE voice client that mirrors the exact Gemini Live audio loop
 used by core/main.py (same model, same sample rates, same mic->cloud /
 cloud->speaker wiring) but imports NONE of PyQt6 / torch / whisper / opencv.

 Mic  : Bluetooth headset  -> Gemini Live (native audio)
 Out  : Bluetooth speaker  <- Gemini Live (native audio)

 WHY miniaudio (not sounddevice): sounddevice needs system PortAudio, which is
 absent on a clean/full macOS install. miniaudio vendors its own backend, so it
 just works with `pip install miniaudio` — no Homebrew, no portaudio.

 NOTE on miniaudio API: this file targets miniaudio >= 1.60, which uses a
 callback-generator model (CaptureDevice / PlaybackDevice + `device.start(gen)`),
 NOT the older get_devices()/InputStream/OutputStream API.

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
import threading
import time
import traceback
from collections import deque
from pathlib import Path

import miniaudio
from google import genai
from google.genai import types

# ── Faithful copy of IRA's live-audio constants (core/main.py lines 129-133) ──
LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-latest"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
SAMPLE_WIDTH        = 2  # SIGNED16

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
    lines = ["Audio devices:"]
    for d in miniaudio.Devices().get_captures():
        lines.append(f"  [IN]  {d['name']}")
    for d in miniaudio.Devices().get_playbacks():
        lines.append(f"  [OUT] {d['name']}")
    return "\n".join(lines)


def pick_devices() -> tuple:
    """Return (input_device_id, output_device_id) chosen by name heuristics."""
    in_id = out_id = None
    for d in miniaudio.Devices().get_captures():
        name = (d["name"] or "").lower()
        if in_id is None and ("headset" in name or "mic" in name or "bluetooth" in name):
            in_id = d["id"]
    for d in miniaudio.Devices().get_playbacks():
        name = (d["name"] or "").lower()
        if out_id is None and ("speaker" in name or "bluetooth" in name or "airplay" in name):
            out_id = d["id"]
    return in_id, out_id


def resolve_device(name_sub: str, want_input: bool):
    name_sub = name_sub.lower()
    devs = (miniaudio.Devices().get_captures() if want_input
            else miniaudio.Devices().get_playbacks())
    for d in devs:
        if name_sub in (d["name"] or "").lower():
            return d["id"]
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--auto-pick", action="store_true", help="pick headset/speaker by name")
    ap.add_argument("--input-name", type=str, default=None, help="input device name substring")
    ap.add_argument("--output-name", type=str, default=None, help="output device name substring")
    ap.add_argument("--input-id", type=str, default=None, help="raw device name to use for input")
    ap.add_argument("--output-id", type=str, default=None, help="raw device name to use for output")
    args = ap.parse_args()

    if args.list_devices:
        print(list_devices())
        return

    # ── Resolve audio devices ──
    in_dev = resolve_device(args.input_name, want_input=True) if args.input_name else None
    out_dev = resolve_device(args.output_name, want_input=False) if args.output_name else None
    if (in_dev is None or out_dev is None) and args.auto_pick:
        a, b = pick_devices()
        in_dev = in_dev or a
        out_dev = out_dev or b

    print(list_devices())
    print(f"[Hub] input_device={'<auto>' if in_dev is None else in_dev}  "
          f"output_device={'<auto>' if out_dev is None else out_dev}")

    # Cross-thread handoff: mic bytes -> Gemini ; Gemini bytes -> speaker
    out_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    loop = asyncio.get_event_loop()
    play_buf = bytearray()
    play_lock = threading.Lock()
    speaking = threading.Event()  # True while IRA is playing audio

    def feed_audio(chunk: bytes) -> None:
        with play_lock:
            play_buf.extend(chunk)
            cap = RECEIVE_SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS * 4  # ~4s cap
            if len(play_buf) > cap:
                del play_buf[:-cap]

    # ── Capture generator (runs in miniaudio capture thread) ──
    def capture_generator():
        while True:
            pcm = yield  # receives raw 16-bit mono PCM bytes
            if speaking.is_set():
                continue  # barge-in guard: don't send mic while IRA is talking
            asyncio.run_coroutine_threadsafe(
                out_queue.put({"data": bytes(pcm), "mime_type": "audio/pcm"}), loop
            )

    # ── Playback generator (runs in miniaudio playback thread) ──
    def playback_generator():
        while True:
            numframes = yield  # miniaudio tells us how many frames it wants
            needed = numframes * SAMPLE_WIDTH * CHANNELS
            with play_lock:
                if len(play_buf) >= needed:
                    data = bytes(play_buf[:needed])
                    del play_buf[:needed]
                    speaking.set()
                else:
                    data = bytes(play_buf)  # emit what we have
                    play_buf.clear()
                    if data:
                        speaking.set()
                    else:
                        speaking.clear()
                        data = b"\x00" * needed  # pad with silence to stay realtime
            yield data

    # Prime generators (must be started before device.start())
    cg = capture_generator()
    next(cg)
    pg = playback_generator()
    next(pg)

    capture_dev = miniaudio.CaptureDevice(
        input_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=CHANNELS,
        sample_rate=SEND_SAMPLE_RATE,
        device_id=in_dev,
    )
    playback_dev = miniaudio.PlaybackDevice(
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=CHANNELS,
        sample_rate=RECEIVE_SAMPLE_RATE,
        device_id=out_dev,
    )

    client = genai.Client(api_key=_get_api_key(), http_options={"api_version": "v1beta"})
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription={},
        input_audio_transcription={},
        system_instruction=SYSTEM_PROMPT,
    )

    async def send_realtime(session):
        while True:
            msg = await out_queue.get()
            await session.send_realtime_input(audio=msg)

    async def receive_audio(session):
        async for response in session.receive():
            if response.data:
                feed_audio(response.data)
            if response.server_content:
                sc = response.server_content
                if sc.output_transcription and sc.output_transcription.text:
                    print(f"IRA: {sc.output_transcription.text}", end="", flush=True)
                if sc.input_transcription and sc.input_transcription.text:
                    print(f"\nYou: {sc.input_transcription.text}")
                if sc.turn_complete:
                    print()

    print("[Hub] Starting 24/7 room voice loop (Ctrl-C to stop)...")
    while True:
        try:
            async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
                print("[Hub] Connected to Gemini Live. Speak into your headset.")
                capture_dev.start(cg)
                playback_dev.start(pg)
                # asyncio.TaskGroup is 3.11+; use gather for 3.9 compatibility
                tasks = [
                    asyncio.ensure_future(send_realtime(session)),
                    asyncio.ensure_future(receive_audio(session)),
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Hub] Session error: {e}")
            traceback.print_exc()
        finally:
            try:
                capture_dev.stop()
            except Exception:
                pass
            try:
                playback_dev.stop()
            except Exception:
                pass
        print("[Hub] Reconnecting in 3s...")
        time.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Hub] Stopped by user.")
