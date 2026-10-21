import subprocess

from app import pipeline
from app.db import Meeting, SessionLocal


def test_recording_routes_real_chunks_finish_settings_and_owner(client, tmp_path, monkeypatch):
    source = tmp_path / "audio.webm"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=2",
                    "-c:a", "libopus", str(source)], check=True)
    queued = []
    monkeypatch.setattr(pipeline, "submit", lambda mid: queued.append(mid))
    response = client.post("/recordings", data={"mime_type": "audio/webm;codecs=opus", "title": "Микрофон",
                                                "meeting_date": "2026-10-07", "deadline_mode": "ask",
                                                "approval_required": "on", "send_tasks_to_assignees": "on"})
    assert response.status_code == 200
    rid = response.json()["id"]
    data = source.read_bytes()
    first, second = data[:100], data[100:]
    assert client.post(f"/recordings/{rid}/chunks/0", content=first).json()["next_sequence"] == 1
    assert client.post(f"/recordings/{rid}/chunks/0", content=first).json()["next_sequence"] == 1
    assert client.post(f"/recordings/{rid}/chunks/1", content=second).json()["next_sequence"] == 2
    other_owner = client.cookies.get("session")
    client.cookies.clear()
    client.post("/login", data={"username": "SYSAI", "password": "pw"})
    cross_device = client.get(f"/recordings/{rid}/status").json()
    assert cross_device["id"] == rid and cross_device["status"] == "recording"
    client.cookies.set("session", other_owner)
    result = client.post(f"/recordings/{rid}/finish").json()
    assert result["url"] == f"/meetings/{result['meeting_id']}"
    assert client.post(f"/recordings/{rid}/finish").json() == result
    assert queued == [result["meeting_id"]]
    with SessionLocal() as s:
        meeting = s.get(Meeting, result["meeting_id"])
        assert meeting.source == "microphone" and meeting.title == "Микрофон"
        assert meeting.options["deadline_mode"] == "ask" and meeting.options["approval_required"]


def test_monitor_can_recover_or_close_a_recording(client, monkeypatch):
    client.post("/login", data={"username": "SYSAI", "password": "pw"})
    response = client.post("/recordings", data={"mime_type": "audio/webm", "title": "Зависшая запись"})
    assert response.status_code == 200
    rid = response.json()["id"]

    monitor = client.get(f"/recordings/{rid}/monitor")
    assert monitor.status_code == 200
    assert 'id="monitor-download"' in monitor.text
    assert 'id="monitor-finish"' in monitor.text
    assert 'id="monitor-cancel"' in monitor.text

    abandoned = client.post(f"/recordings/{rid}/abandon")
    assert abandoned.status_code == 200
    assert abandoned.json()["status"] == "error"
    assert client.get(f"/recordings/{rid}/status").json()["status"] == "error"

    cancelled = client.post(f"/recordings/{rid}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_recording_routes_require_login_and_validate_date(client):
    client.get("/logout")
    assert client.post("/recordings", data={"mime_type": "audio/webm"}, follow_redirects=False).status_code == 303
    client.post("/login", data={"username": "SYSAI", "password": "pw"})
    assert client.post("/recordings", data={"mime_type": "audio/webm", "meeting_date": "invalid"}).status_code == 400
