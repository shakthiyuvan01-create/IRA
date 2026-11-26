"""IRA audio device handling — enumeration, name-based resolution, headset
auto-detection and live hot-plug re-routing.

Why this module exists
----------------------
IRA previously persisted audio devices as PortAudio integer *indices*. That is
fragile: Bluetooth headset indices shift on every reconnect, so a saved index
points at the wrong (or a non-existent) device after the headset power-cycles.

This module is the single source of truth for audio device selection. It:

  * enumerates input (mic) and output (speaker/headphone) devices,
  * resolves a *persisted friendly name* (e.g. "Headphones (3.0 BT)") to a
    live index every time it is needed, so reconnects never break routing,
  * detects a headset being plugged in / pulled out,
  * exposes a monitor that the voice loop polls to re-route input + output
    on the fly without restarting IRA,
  * falls back to Windows Core Audio (WASAPI) when PortAudio cannot open a
    Bluetooth Hands-Free headset mic (the exact failure documented in
    core/wasapi_mic.py: "Unanticipated host error").

All resolution is defensive: a missing/renamed device silently falls back to
the system default instead of raising.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import sounddevice as sd


# Device names that are OS meta-devices, never a real headset.
_META_KEYWORDS = ("sound mapper", "primary sound", "primary sound driver")


def list_devices(kind: str = "all") -> list[dict]:
    """Return input/output/all devices as plain dicts.

    kind: "input" | "output" | "all". Each entry:
        {"index", "name", "hostapi", "samplerate", "channels"}
    channels is max_input_channels for input, max_output_channels for output.
    """
    out: list[dict] = []
    try:
        raw = sd.query_devices()
    except Exception:
        return out
    for i, d in enumerate(raw):
        max_in = int(d.get("max_input_channels", 0) or 0)
        max_out = int(d.get("max_output_channels", 0) or 0)
        if kind == "input" and max_in <= 0:
            continue
        if kind == "output" and max_out <= 0:
            continue
        # Skip OS meta-devices unless caller explicitly enumerates everything.
        name = str(d.get("name", ""))
        if kind != "all" and any(k in name.lower() for k in _META_KEYWORDS):
            continue
        out.append({
            "index": i,
            "name": name,
            "hostapi": int(d.get("hostapi", -1)),
            "samplerate": int(d.get("default_samplerate", 44100) or 44100),
            "channels": max_in if kind == "input" else (max_out if kind == "output" else max(max_in, max_out)),
        })
    return out


def _is_meta(name: str) -> bool:
    return any(k in name.lower() for k in _META_KEYWORDS)


def resolve_device(spec, kind: str) -> Optional[int]:
    """Resolve a persisted device spec to a live PortAudio index.

    spec may be:
      * None / "default" / ""  -> None (system default)
      * int                   -> returned as-is (validated against capability)
      * str (friendly name)   -> first device whose name contains the spec
                                 (case-insensitive), capability-filtered by kind
    Returns None when unresolved so callers can use the system default.
    """
    if spec is None:
        return None
    kind = kind.lower()
    if isinstance(spec, str):
        s = spec.strip().lower()
        if not s or s == "default":
            return None
        try:
            return int(s)
        except ValueError:
            pass
        for d in list_devices(kind):
            if s in d["name"].lower():
                return d["index"]
        return None
    if isinstance(spec, int):
        # Validate the index still exists and is capable.
        for d in list_devices(kind):
            if d["index"] == spec:
                return spec
        return None
    return None


def _looks_like_headset(name: str) -> bool:
    n = name.lower()
    if _is_meta(n):
        return False
    # Prefer real headset endpoints; the WDM-KS "@System32\\...bthhfenum" entries
    # are the Bluetooth Hands-Free profile which we only want as a last resort.
    is_handsfree = "bthhfenum" in n or "hands-free" in n
    if "headset" in n:
        return not is_handsfree
    if "headphone" in n:
        return not is_handsfree
    return False


def pick_headset(kind: str) -> Optional[dict]:
    """Return the best connected headset device for kind ('input'|'output').

    For input this is a headset *microphone*; for output a headset *headphone*.
    Returns None if no headset is currently connected (caller falls back to
    default / laptop mic).
    """
    cands = [d for d in list_devices(kind) if _looks_like_headset(d["name"])]
    if not cands:
        return None
    # Prefer higher sample-rate endpoints (better fidelity) among matches.
    cands.sort(key=lambda d: d["samplerate"], reverse=True)
    return cands[0]


def headset_connected(kind: str) -> bool:
    return pick_headset(kind) is not None


def _device_signature() -> tuple:
    """A stable fingerprint of the current device topology, used to detect
    plug/unplug events cheaply."""
    ins = tuple(d["name"] for d in list_devices("input"))
    outs = tuple(d["name"] for d in list_devices("output"))
    return (ins, outs)


class DeviceMonitor:
    """Background poller that watches for audio device changes.

    on_change(new_inputs, new_outputs)      – fired whenever the device set changes.
    on_headset(kind, plugged, name)         – fired when a headset is plugged in
                                               (plugged=True) or removed (False).

    Started once by the voice loop. Stop with .stop(). Uses a daemon thread so
    it never blocks shutdown.
    """

    def __init__(
        self,
        interval_s: float = 2.0,
        on_change: Optional[Callable[[list, list], None]] = None,
        on_headset: Optional[Callable[[str, bool, Optional[str]], None]] = None,
    ):
        self._interval = interval_s
        self._on_change = on_change
        self._on_headset = on_headset
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sig = _device_signature()
        self._headset_in = headset_connected("input")
        self._headset_out = headset_connected("output")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._sig = _device_signature()
        self._headset_in = headset_connected("input")
        self._headset_out = headset_connected("output")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            time.sleep(self._interval)
            if self._stop.is_set():
                break
            try:
                sig = _device_signature()
            except Exception:
                continue
            if sig != self._sig:
                self._sig = sig
                if self._on_change:
                    try:
                        self._on_change(
                            [d["name"] for d in list_devices("input")],
                            [d["name"] for d in list_devices("output")],
                        )
                    except Exception:
                        pass
                # Per-direction headset plug/unplug detection.
                hin = headset_connected("input")
                hout = headset_connected("output")
                if hin != self._headset_in and self._on_headset:
                    self._headset_in = hin
                    name = pick_headset("input")["name"] if hin else None
                    try:
                        self._on_headset("input", hin, name)
                    except Exception:
                        pass
                if hout != self._headset_out and self._on_headset:
                    self._headset_out = hout
                    name = pick_headset("output")["name"] if hout else None
                    try:
                        self._on_headset("output", hout, name)
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# Microphone capture with WASAPI fallback
# ---------------------------------------------------------------------------

class MicStream:
    """Streaming mic source that feeds raw int16 frames to a callback.

    Primary path is PortAudio (sd.InputStream, callback mode) — lowest latency.
    If PortAudio cannot open the requested device (the classic Bluetooth
    Hands-Free "Unanticipated host error"), it transparently falls back to
    Windows Core Audio via core.wasapi_mic, running a poller thread that calls
    the same callback with captured frames.

    The callback signature matches sd.InputStream:
        callback(indata_bytes, frames, time_info, status)
    where indata_bytes is a bytes object of int16 mono samples.
    """

    def __init__(
        self,
        callback: Callable,
        samplerate: int = 16000,
        channels: int = 1,
        dtype: str = "int16",
        blocksize: int = 1024,
        device=None,
    ):
        self._callback = callback
        self._samplerate = samplerate
        self._channels = channels
        self._dtype = dtype
        self._blocksize = blocksize
        self._device = device
        self._pa_stream = None
        self._wasapi = None
        self._wasapi_thread: Optional[threading.Thread] = None
        self._wasapi_stop = threading.Event()
        self._using_wasapi = False

    @property
    def using_wasapi(self) -> bool:
        return self._using_wasapi

    def start(self) -> None:
        # Try PortAudio first.
        try:
            self._pa_stream = sd.InputStream(
                samplerate=self._samplerate,
                channels=self._channels,
                dtype=self._dtype,
                blocksize=self._blocksize,
                callback=self._pa_callback,
                device=self._device,
            )
            self._pa_stream.start()
            dev = self._device if self._device is not None else "default"
            print(f"[Mic] PortAudio input stream open (device={dev})")
            return
        except Exception as e:
            print(f"[Mic] PortAudio input failed ({e}); trying WASAPI fallback…")
            try:
                self._pa_stream = None
                self._start_wasapi()
                return
            except Exception as e2:
                raise RuntimeError(f"Mic capture unavailable: PortAudio={e}; WASAPI={e2}")

    def _pa_callback(self, indata, frames, time_info, status):
        try:
            self._callback(indata.tobytes(), frames, time_info, status)
        except Exception:
            pass

    def _start_wasapi(self) -> None:
        from core.wasapi_mic import WasapiMicCapture, WasapiMicError
        # WASAPI always captures from the *default* input endpoint, which is
        # exactly what we want for a Bluetooth headset mic: when the headset is
        # the default recording device, WASAPI opens it where PortAudio cannot.
        try:
            cap = WasapiMicCapture(samplerate=self._samplerate, channels=self._channels)
        except WasapiMicError as e:
            raise RuntimeError(f"WASAPI init failed: {e}")
        self._wasapi = cap
        self._using_wasapi = True
        self._wasapi_stop.clear()
        self._wasapi_thread = threading.Thread(target=self._wasapi_poll, args=(cap,), daemon=True)
        self._wasapi_thread.start()
        print("[Mic] WASAPI input stream open (default endpoint)")

    def _wasapi_poll(self, cap: "object") -> None:
        while not self._wasapi_stop.is_set():
            try:
                chunk = cap.read_chunk(timeout_s=0.2)
            except Exception:
                chunk = b""
            if not chunk:
                continue
            frames = len(chunk) // (self._channels * 2)
            try:
                self._callback(chunk, frames, None, None)
            except Exception:
                pass

    def stop(self) -> None:
        if self._pa_stream is not None:
            try:
                self._pa_stream.stop()
                self._pa_stream.close()
            except Exception:
                pass
            self._pa_stream = None
        if self._wasapi is not None:
            self._wasapi_stop.set()
            try:
                self._wasapi.stop()
            except Exception:
                pass
            self._wasapi = None
            self._using_wasapi = False
