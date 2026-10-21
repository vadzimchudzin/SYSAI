"""Static files carry a content version, so a deploy never shows new pages with old styles from the browser cache."""
import re

from app.main import asset


def test_pages_link_versioned_styles_and_scripts(client):
    html = client.get("/").text
    for name in ("style.css", "fonts.css", "landing.js"):
        assert re.search(rf'/static/{re.escape(name)}\?v=[0-9a-f]{{10}}"', html), name
    cabinet = client.get("/app").text
    assert re.search(r'/static/app\.js\?v=[0-9a-f]{10}"', cabinet)


def test_the_versioned_url_serves_the_file(client):
    assert client.get(asset("style.css")).status_code == 200
    font = client.get("/static/fonts/inter-normal-cyr.woff2")
    assert font.status_code == 200 and font.headers["content-type"].startswith("font/woff2")


def test_recording_script_marks_abandoned_sessions_for_recovery(client):
    response = client.get("/static/recording.js")
    assert response.status_code == 200
    assert "pagehide" in response.text
    assert "/abandon" in response.text
    assert "pollServerStopRequest" in response.text


def test_recording_monitor_has_targeted_stop_control(client):
    client.post("/login", data={"username": "SYSAI", "password": "pw"})
    response = client.post("/recordings", data={"mime_type": "audio/webm", "title": "Тестовая запись"})
    rid = response.json()["id"]
    monitor = client.get(f"/recordings/{rid}/monitor")
    assert monitor.status_code == 200
    assert 'id="monitor-stop"' in monitor.text
    assert 'aria-label="Остановить эту запись"' in monitor.text


def test_the_version_follows_the_content():
    assert asset("style.css") != asset("app.js")
    assert asset("style.css") == asset("style.css")
