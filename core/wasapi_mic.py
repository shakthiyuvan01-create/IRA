"""WASAPI microphone capture via Windows Core Audio (comtypes).

Why this exists:
    IRA's normal audio engine is PortAudio (sounddevice). On this machine
    PortAudio cannot open a Bluetooth Hands-Free (WDM-KS) headset microphone
    at all ("Unanticipated host error"). Windows Core Audio (WASAPI) -- the
    same path Chrome/Zoom use -- CAN open the headset when it is the system
    default recording device. This module captures from the *default* input
    endpoint and yields 16 kHz mono int16 frames, compatible with the rest of
    IRA's voice pipeline.

If anything here fails to import/initialise, callers should fall back to the
normal sounddevice path so the assistant keeps working.
"""
import ctypes
import comtypes
import comtypes.client  # required for CreateObject
from comtypes import IUnknown, GUID, COMMETHOD, POINTER, c_int, c_uint, c_void_p, byref, cast
from ctypes import (
    c_uint16, c_uint32, c_uint64, c_void_p as LPVOID, c_int16,
    addressof, memmove, windll, CFUNCTYPE,
)

# GUIDs
CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioClient = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C1E3E5}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C397CDAFB}")
KSDATAFORMAT_SUBTYPE_PCM = GUID("{00000001-0000-0010-8000-00AA00389B71}")

# Constants
AUDCLNT_SHAREMODE_SHARED = 1
AUDCLNT_STREAMFLAGS_NOWAIT = 0x00000002
WAVE_FORMAT_PCM = 1
DEVICE_STATE_ACTIVE = 0x00000001
eCapture = 1  # EDataFlow::eCapture


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", c_uint16),
        ("nChannels", c_uint16),
        ("nSamplesPerSec", c_uint32),
        ("nAvgBytesPerSec", c_uint32),
        ("nBlockAlign", c_uint16),
        ("wBitsPerSample", c_uint16),
        ("cbSize", c_uint16),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = IID_IMMDeviceEnumerator
    _methods_ = [
        COMMETHOD([], ctypes.HRESULT, "EnumAudioEndpoints",
                  (["in"], c_int, "dataFlow"),
                  (["in"], c_uint32, "dwStateMask"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppDevices")),
        COMMETHOD([], ctypes.HRESULT, "GetDefaultAudioEndpoint",
                  (["in"], c_int, "dataFlow"),
                  (["in"], c_uint32, "dwRole"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppEndpoint")),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], ctypes.HRESULT, "Activate",
                  (["in"], GUID, "iid"),
                  (["in"], c_int, "dwClsCtx"),
                  (["in"], c_void_p, "pActivationParams"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = IID_IAudioCaptureClient
    _methods_ = [
        COMMETHOD([], ctypes.HRESULT, "GetBuffer",
                  (["out"], POINTER(LPVOID), "ppData"),
                  (["out"], POINTER(c_uint32), "pNumFramesToRead"),
                  (["out"], POINTER(c_uint32), "pdwFlags"),
                  (["out"], POINTER(c_uint64), "pu64DevicePosition"),
                  (["out"], POINTER(c_uint64), "pu64QPCPosition")),
        COMMETHOD([], ctypes.HRESULT, "ReleaseBuffer",
                  (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD([], ctypes.HRESULT, "GetNextPacketSize",
                  (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
    ]


class IAudioClient(IUnknown):
    _iid_ = IID_IAudioClient
    _methods_ = [
        COMMETHOD([], ctypes.HRESULT, "Initialize",
                  (["in"], c_int, "ShareMode"),
                  (["in"], c_uint32, "StreamFlags"),
                  (["in"], c_uint64, "hnsBufferDuration"),
                  (["in"], c_uint64, "hnsPeriodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["in"], c_void_p, "AudioSessionGuid")),
        COMMETHOD([], ctypes.HRESULT, "GetBufferSize",
                  (["out"], POINTER(c_uint32), "pNumBufferFrames")),
        COMMETHOD([], ctypes.HRESULT, "GetCurrentPadding",
                  (["out"], POINTER(c_uint32), "pNumPaddingFrames")),
        COMMETHOD([], ctypes.HRESULT, "GetService",
                  (["in"], GUID, "riid"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
        COMMETHOD([], ctypes.HRESULT, "Start"),
        COMMETHOD([], ctypes.HRESULT, "Stop"),
    ]


class WasapiMicError(Exception):
    pass


def _com_error(hr, where):
    raise WasapiMicError(f"WASAPI {where} failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")


class WasapiMicCapture:
    """Capture 16 kHz mono int16 PCM from the default Windows input device."""

    def __init__(self, samplerate: int = 16000, channels: int = 1):
        self.samplerate = samplerate
        self.channels = channels
        self._client = None
        self._capture = None
        self._buffer_frames = 0
        self._initialize()

    def _initialize(self):
        try:
            enum = comtypes.client.CreateObject(
                CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator
            )
        except Exception as e:
            raise WasapiMicError(f"cannot create MMDeviceEnumerator: {e}")

        endpoint = enum.GetDefaultAudioEndpoint(eCapture, DEVICE_STATE_ACTIVE)
        device = cast(endpoint, POINTER(IMMDevice))

        client_ptr = device.Activate(IID_IAudioClient, 1 | 4, None)  # CLSCTX_ALL
        self._client = client_ptr

        fmt = WAVEFORMATEX()
        fmt.wFormatTag = WAVE_FORMAT_PCM
        fmt.nChannels = self.channels
        fmt.nSamplesPerSec = self.samplerate
        fmt.wBitsPerSample = 16
        fmt.nBlockAlign = fmt.nChannels * fmt.wBitsPerSample // 8
        fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign
        fmt.cbSize = 0
        self._format = fmt

        hr = self._client.Initialize(
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_NOWAIT,
            0,
            0,
            byref(fmt),
            None,
        )
        if hr != 0:
            _com_error(hr, "Initialize")

        svc = POINTER(IUnknown)()
        hr = self._client.GetService(IID_IAudioCaptureClient, byref(svc))
        if hr != 0:
            _com_error(hr, "GetService")
        self._capture = cast(svc, POINTER(IAudioCaptureClient))

        buf_size = c_uint32()
        hr = self._client.GetBufferSize(byref(buf_size))
        if hr != 0:
            _com_error(hr, "GetBufferSize")
        self._buffer_frames = buf_size.value

    def start(self):
        hr = self._client.Start()
        if hr != 0:
            _com_error(hr, "Start")

    def stop(self):
        try:
            if self._client is not None:
                self._client.Stop()
        except Exception:
            pass

    def read_packet(self):
        """Return one packet of int16 bytes (mono), or b'' if none ready."""
        flags = c_uint32()
        nframes = c_uint32()
        data = LPVOID()
        hr = self._capture.GetBuffer(byref(data), byref(nframes), byref(flags), None, None)
        if hr != 0:
            return b""
        n = nframes.value
        if n == 0:
            self._capture.ReleaseBuffer(0)
            return b""
        raw = ctypes.string_at(data, n * self.channels * 2)
        self._capture.ReleaseBuffer(n)
        return raw

    def read_chunk(self, timeout_s: float = 0.0):
        """Read available audio; block up to timeout_s if nothing yet."""
        import time
        deadline = time.time() + timeout_s
        while True:
            pkt = self.read_packet()
            if pkt:
                return pkt
            if time.time() >= deadline:
                return b""
            time.sleep(0.005)
