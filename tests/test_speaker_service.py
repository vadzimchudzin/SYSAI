"""The voice service's features are SpeechBrain's: the mel table is read in its real layout, the numbers match."""
import importlib.util
import pathlib
import sys
import types

import numpy as np
import pytest

from app import speaker_id

SERVICE = pathlib.Path(__file__).resolve().parents[1] / "speaker-service" / "app.py"


@pytest.fixture(scope="module")
def service():
    if "vercel" not in sys.modules:  # the Vercel SDK is the service's own dependency, not the app's
        stub = types.ModuleType("vercel")
        stub.blob = types.ModuleType("vercel.blob")
        sys.modules["vercel"] = stub
    spec = importlib.util.spec_from_file_location("sysai_speaker_service", SERVICE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mel_filters():
    """SpeechBrain's Filterbank(n_mels=80) for 16 kHz and n_fft 400: [80 filters × 201 bins] (matches to 1e-5)."""
    to_mel = lambda hz: 2595 * np.log10(1 + hz / 700.0)  # noqa: E731
    to_hz = lambda mel: 700 * (10 ** (mel / 2595) - 1)  # noqa: E731
    hz = to_hz(np.linspace(to_mel(0.0), to_mel(8000.0), 82))
    band, central = (hz[1:] - hz[:-1])[:-1], hz[1:-1]
    slope = (np.linspace(0, 8000, 201)[None, :] - central[:, None]) / band[:, None]
    return np.maximum(0.0, np.minimum(slope + 1.0, 1.0 - slope)).astype(np.float32)


def signal():
    sr = 16000
    t = np.arange(int(1.3 * sr)) / sr
    x = (0.3 * np.sin(2 * np.pi * 220 * t) + 0.2 * np.sin(2 * np.pi * (300 + 900 * t) * t) * (t > 0.4)
         + 0.05 * np.sin(2 * np.pi * 3100 * t) * np.cos(2 * np.pi * 3 * t)).astype(np.float32)
    x[:1600] = 0.0  # digital silence: the −80 dB floor of top_db
    return x


def test_mel_table_is_read_as_speechbrain_stores_it(service):
    filters = mel_filters()
    stored = filters.T.ravel()  # fbank-80x201-f32.bin: 201 bins × 80 filters, row by row
    assert np.array_equal(service._filters(stored), filters)
    with pytest.raises(RuntimeError):  # the other layout scatters every filter: refused, not used
        service._filters(filters.ravel())
    with pytest.raises(RuntimeError):
        service._filters(np.zeros(100, dtype=np.float32))


def test_features_match_speechbrain(service):
    features = service._fbank_features(signal(), mel_filters())
    assert features.shape == (131, 80)
    # SpeechBrain 1.0.3: Fbank(n_mels=80) then InputNormalization(norm_type="sentence", std_norm=False)
    golden = {(0, 0): -30.1135, (5, 3): -33.0228, (20, 10): 4.0050, (40, 25): 27.6925, (60, 50): -11.0766,
              (100, 70): 0.8863, (129, 79): 1.3799}
    for (row, column), value in golden.items():
        assert features[row, column] == pytest.approx(value, abs=0.02), (row, column)
    assert np.abs(features.mean(axis=0)).max() < 1e-3


def test_new_prints_are_a_new_model_version(service):
    assert service.MODEL_VERSION not in speaker_id.OUTDATED_MODELS
    assert "speechbrain-ecapa-voxceleb-onnx-a9cb932" in speaker_id.OUTDATED_MODELS


class _Answer:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_an_outdated_voice_service_is_refused(monkeypatch, tmp_path):
    """A service not redeployed since the fix gives prints where every voice is alike: they are not used."""
    clip = tmp_path / "part.mp3"
    clip.write_bytes(b"mp3")
    answer = {"model_version": "speechbrain-ecapa-voxceleb-onnx-a9cb932",
              "speakers": {"Спикер 1": {"embedding": [1.0] + [0.0] * 191, "duration_seconds": 5}},
              "embedding": [1.0] + [0.0] * 191, "duration_seconds": 8, "waveform": None, "sample_blob_path": None}
    monkeypatch.setattr(speaker_id, "_voice_service_request_url", lambda path: "https://voice.example" + path)
    monkeypatch.setattr(speaker_id, "_voice_service_headers", lambda: {})
    monkeypatch.setattr(speaker_id.httpx, "post", lambda *args, **kwargs: _Answer(answer))
    with pytest.raises(speaker_id.OutdatedServiceError, match="Передеплойте speaker-service"):
        speaker_id.embed_speakers(clip, [{"speaker": "Спикер 1", "start": 0, "end": 5}])
    with pytest.raises(speaker_id.OutdatedServiceError):
        speaker_id.embed_voice_sample(clip, 1)
    answer["model_version"] = "speechbrain-ecapa-voxceleb-onnx-a9cb932-fbank2"
    assert speaker_id.embed_speakers(clip, [{"speaker": "Спикер 1", "start": 0, "end": 5}])["speakers"]
    assert speaker_id.embed_voice_sample(clip, 1)["model_version"].endswith("-fbank2")
