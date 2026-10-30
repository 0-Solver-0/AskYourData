"""Question dictée : appel de Muse Voice Transcribe (faux transport) et route /api/v1/transcribe."""

import json
import struct

import pytest

from api_service import voice

ENV = {"META_API_KEY": "sk-test-secret", "VOICE_LANGUAGES": "English, French"}


def wav(seconds=1.0, rate=16000):
    n = int(seconds * rate)
    header = (b"RIFF" + struct.pack("<I", 36 + 2 * n) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
              + b"data" + struct.pack("<I", 2 * n))
    return header + b"\0\0" * n


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def test_transcribe_sends_the_documented_request():
    calls = []

    def post(url, headers, files, timeout):
        calls.append((url, headers, files))
        return FakeResp(200, {"sessionId": "s1", "transcript": "  Overdue vendor bills\n by vendor ", "audioDurationMs": 7200})
    out = voice.transcribe(wav(), env=ENV, post=post)
    assert out["text"] == "Overdue vendor bills by vendor" and out["audio_ms"] == 7200
    assert out["model"] == "muse-voice-transcribe-1.0" and out["cost_usd"] == pytest.approx(7 / 3600 * 0.18, abs=1e-6)
    url, headers, files = calls[0]
    assert url == "https://api.meta.ai/v1/asr/transcribe" and headers["Authorization"] == "Bearer sk-test-secret"
    settings = json.loads(files["request"][1])
    assert settings == {"mode": "PUSH_TO_TALK", "model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV",
                        "languageBias": ["English", "French"]}
    assert files["request"][2] == "application/json" and files["audio"][2] == "audio/wav"


def test_errors_are_clear_and_never_show_the_key():
    with pytest.raises(voice.VoiceError, match="META_API_KEY"):
        voice.transcribe(wav(), env={})
    with pytest.raises(voice.VoiceError, match="WAV"):
        voice.transcribe(b"OggS....", env=ENV)

    def refused(*a, **k):
        return FakeResp(401, {"error": "invalid key sk-test-secret"})
    with pytest.raises(voice.VoiceError) as exc:
        voice.transcribe(wav(), env=ENV, post=refused)
    assert "401" in str(exc.value) and "sk-test-secret" not in str(exc.value)


def test_routes(monkeypatch):
    from api_service import app as appmod
    client = appmod.app.test_client()
    assert b"What do you want" in client.get("/").data
    assert client.get("/static/app.js").status_code == 200
    monkeypatch.setattr(voice, "transcribe", lambda data: {"text": "hello", "audio_ms": 1000, "elapsed_ms": 5,
                                                            "model": "m", "cost_usd": 0})
    resp = client.post("/api/v1/transcribe", data={"audio": (__import__("io").BytesIO(wav()), "q.wav")})
    assert resp.status_code == 200 and resp.get_json()["text"] == "hello"
    monkeypatch.setattr(voice, "transcribe", lambda data: (_ for _ in ()).throw(voice.VoiceError("bad audio")))
    assert client.post("/api/v1/transcribe", data=b"x").status_code == 400
