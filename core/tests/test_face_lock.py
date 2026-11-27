"""Face-lock storage tests (no camera or model download needed)."""
import json

import numpy as np
import pytest

from core import face_lock as fl


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setenv("IRA_FACE_LOCK_DIR", str(tmp_path))


def test_password_seeded_as_hash_only():
    fl.ensure_password_seeded()
    rec = json.loads(fl._password_file().read_text(encoding="utf-8"))
    assert set(rec) == {"algo", "iterations", "salt", "hash"}
    assert rec["hash"] == fl._SEED["hash"]
    assert not fl.check_password("")
    assert not fl.check_password("wrong-password")


def test_set_password_replaces_seed():
    fl.set_password("new-secret")
    assert fl.check_password("new-secret")
    assert not fl.check_password("new-secret ")
    assert fl._load_password_record()["hash"] != fl._SEED["hash"]


def test_enrollment_round_trip_and_reset():
    assert not fl.is_enrolled()
    samples = [np.random.rand(128).astype(np.float32) for _ in range(3)]
    fl.save_enrollment(samples)
    loaded = fl.load_enrollment()
    assert loaded.shape == (3, 128)
    assert fl.reset_face() and not fl.is_enrolled()


def test_similarity_prefers_enrolled_face():
    rng = np.random.default_rng(0)
    me = rng.normal(size=128).astype(np.float32)
    enrolled = np.stack([me + rng.normal(scale=0.1, size=128) for _ in range(4)]).astype(np.float32)
    stranger = rng.normal(size=128).astype(np.float32)
    engine = fl.FaceEngine.__new__(fl.FaceEngine)   # similarity() needs no models
    assert engine.similarity(me, enrolled) > fl.MATCH_THRESHOLD
    assert engine.similarity(stranger, enrolled) < fl.MATCH_THRESHOLD
