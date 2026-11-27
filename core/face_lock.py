"""Face lock: enroll the owner's face on first run, match it on every launch after.

Engine only (no Qt). The lock screen lives in core/face_lock_ui.py.

How it works
  * Detection:   OpenCV YuNet   (cv2.FaceDetectorYN)
  * Recognition: OpenCV SFace   (cv2.FaceRecognizerSF), 128-d embeddings,
                 compared with cosine similarity.
  Both ship with plain `opencv-python` (>= 4.8); only the two small ONNX model
  files are fetched once from the official OpenCV model zoo and cached locally.

Everything is stored on this machine only, in the IRA app-data folder:
  Windows : %APPDATA%\\IRA\\face_lock\\
  macOS   : ~/Library/Application Support/IRA/face_lock/
  Linux   : ~/.config/IRA/face_lock/

  face.npz       enrolled face embeddings (numbers, not a photo)
  password.json  PBKDF2-SHA256 hash + salt of the fallback password
  models/        the two ONNX model files

Maintenance from a terminal (run from the IRA folder):
  python -m core.face_lock --reset-face      forget the enrolled face (re-enroll next launch)
  python -m core.face_lock --set-password    change the fallback password
  python -m core.face_lock --where           print the data folder
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import sys
import urllib.request
from pathlib import Path

try:
    import cv2
    import numpy as np
    _CV2 = True
except Exception:  # pragma: no cover - IRA always ships opencv, but stay importable
    cv2 = None  # type: ignore[assignment]
    np = None   # type: ignore[assignment]
    _CV2 = False


# ── Locations ────────────────────────────────────────────────────────────────

def data_dir() -> Path:
    override = os.environ.get("IRA_FACE_LOCK_DIR")
    if override:
        base = Path(override)
    else:
        system = platform.system()
        if system == "Windows":
            base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "IRA"
        elif system == "Darwin":
            base = Path.home() / "Library" / "Application Support" / "IRA"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "IRA"
        base = base / "face_lock"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _face_file() -> Path:
    return data_dir() / "face.npz"


def _password_file() -> Path:
    return data_dir() / "password.json"


def _models_dir() -> Path:
    d = data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Fallback password (hashed, never stored in plain text) ──────────────────

_PBKDF2_ITERS = 200_000
# Seed hash for the default password chosen by the owner. Only the salt and the
# PBKDF2 hash live here; the plain password does not appear anywhere in the code.
_SEED = {
    "algo": "pbkdf2_sha256",
    "iterations": _PBKDF2_ITERS,
    "salt": "3eba6ab74683ba292024bf19e2460808",
    "hash": "281effe5d8cc0ef3dfce4ab70d01412848d718b69dd83e15df77880a917025c7",
}


def ensure_password_seeded() -> None:
    """Write the default password hash on first run (never overwrites)."""
    pf = _password_file()
    if not pf.exists():
        pf.write_text(json.dumps(_SEED, indent=2), encoding="utf-8")


def _load_password_record() -> dict:
    ensure_password_seeded()
    try:
        rec = json.loads(_password_file().read_text(encoding="utf-8"))
        if rec.get("salt") and rec.get("hash"):
            return rec
    except Exception:
        pass
    return dict(_SEED)


def check_password(password: str) -> bool:
    rec = _load_password_record()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(rec["salt"]),
        int(rec.get("iterations", _PBKDF2_ITERS)),
    )
    return hmac.compare_digest(digest.hex(), rec["hash"])


def set_password(password: str) -> None:
    if not password:
        raise ValueError("Password cannot be empty.")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    rec = {"algo": "pbkdf2_sha256", "iterations": _PBKDF2_ITERS,
           "salt": salt.hex(), "hash": digest.hex()}
    _password_file().write_text(json.dumps(rec, indent=2), encoding="utf-8")


# ── Enrolled face ────────────────────────────────────────────────────────────

def is_enrolled() -> bool:
    return _face_file().exists()


def save_enrollment(embeddings: list) -> None:
    arr = np.asarray(embeddings, dtype=np.float32).reshape(len(embeddings), -1)
    with open(_face_file(), "wb") as fh:
        np.savez(fh, embeddings=arr)


def load_enrollment():
    if not is_enrolled():
        return None
    try:
        with np.load(_face_file()) as data:
            return data["embeddings"].astype(np.float32)
    except Exception:
        return None


def reset_face() -> bool:
    f = _face_file()
    if f.exists():
        f.unlink()
        return True
    return False


# ── Models ───────────────────────────────────────────────────────────────────

_ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
MODELS = {
    "detector": {
        "file": "face_detection_yunet_2023mar.onnx",
        "url": f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    },
    "recognizer": {
        "file": "face_recognition_sface_2021dec.onnx",
        "url": f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    },
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_models(progress=None) -> dict:
    """Return {"detector": Path, "recognizer": Path}, downloading once if missing.

    Raises RuntimeError with a short, speakable message if they can't be fetched.
    """
    out = {}
    for key, spec in MODELS.items():
        path = _models_dir() / spec["file"]
        if not path.exists() or _sha256(path) != spec["sha256"]:
            if progress:
                progress(f"Downloading face model ({key})…")
            tmp = path.with_suffix(".part")
            try:
                req = urllib.request.Request(spec["url"], headers={"User-Agent": "IRA-face-lock"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        fh.write(chunk)
                if _sha256(tmp) != spec["sha256"]:
                    raise RuntimeError("checksum mismatch")
                tmp.replace(path)
            except Exception as e:
                try:
                    tmp.unlink()
                except Exception:
                    pass
                raise RuntimeError(f"Couldn't download the face model ({e}). Use the blue dot for password.")
        out[key] = path
    return out


# ── Engine ───────────────────────────────────────────────────────────────────

# SFace's published cosine threshold is 0.363; we sit a little above it to
# favour rejecting strangers over accepting look-alikes.
MATCH_THRESHOLD = 0.42
ENROLL_SAMPLES = 8


class FaceEngine:
    """Detect the single largest face in a BGR frame and embed / match it."""

    def __init__(self, model_paths: dict):
        if not _CV2 or not hasattr(cv2, "FaceDetectorYN"):
            raise RuntimeError("OpenCV 4.8+ is needed for face lock. Run: pip install -U opencv-python")
        self._det = cv2.FaceDetectorYN.create(str(model_paths["detector"]), "", (320, 320), 0.85, 0.3, 5000)
        self._rec = cv2.FaceRecognizerSF.create(str(model_paths["recognizer"]), "")
        self._size = (0, 0)

    def detect(self, frame):
        """Return (face_row | None, face_count). face_row is YuNet's 15-float row."""
        h, w = frame.shape[:2]
        if (w, h) != self._size:
            self._det.setInputSize((w, h))
            self._size = (w, h)
        _, faces = self._det.detect(frame)
        if faces is None or len(faces) == 0:
            return None, 0
        best = max(faces, key=lambda f: f[2] * f[3])
        return best, len(faces)

    def embed(self, frame, face_row):
        aligned = self._rec.alignCrop(frame, face_row)
        feat = self._rec.feature(aligned)
        return feat.reshape(-1).astype(np.float32)

    def similarity(self, feat, enrolled) -> float:
        """Best cosine similarity of feat against any enrolled sample."""
        f = feat / (np.linalg.norm(feat) + 1e-9)
        e = enrolled / (np.linalg.norm(enrolled, axis=1, keepdims=True) + 1e-9)
        return float(np.max(e @ f))


def camera_source():
    """(index, backend) — reuses the camera IRA already detected for vision."""
    index, backend = 0, 0
    try:
        from actions.screen_processor import _get_camera_index, _cv2_backend
        index, backend = _get_camera_index(), _cv2_backend()
    except Exception:
        if _CV2:
            backend = {"Windows": cv2.CAP_DSHOW, "Darwin": cv2.CAP_AVFOUNDATION}.get(
                platform.system(), cv2.CAP_ANY)
    return index, backend


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    if "--where" in argv:
        print(data_dir())
        return 0
    if "--reset-face" in argv:
        print("Enrolled face removed. IRA will enroll again next launch." if reset_face()
              else "No face was enrolled.")
        return 0
    if "--set-password" in argv:
        import getpass
        p1 = getpass.getpass("New IRA password: ")
        p2 = getpass.getpass("Repeat: ")
        if not p1 or p1 != p2:
            print("Passwords didn't match. Nothing changed.")
            return 1
        set_password(p1)
        print("Password updated.")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
