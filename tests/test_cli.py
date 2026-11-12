from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from elisa_bot.__main__ import _monitor_line, main
from elisa_bot.config import Settings
from elisa_bot.service import monitor_ping_error


BODIES = {"abc": b"OK", "boom": b"", "unknown": b"OK (not found)"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits.append(self.path)
        body = BODIES.get(self.path.rsplit("/", 1)[-1], b"OK")
        self.send_response(500 if self.path.endswith("/boom") else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def monitor():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.hits = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def env(tmp_path, **values) -> list[str]:
    path = tmp_path / "env"
    path.write_text("".join(f"{k}={v}\n" for k, v in {"DATA_DIR": tmp_path / "data", **values}.items()))
    return ["--env-file", str(path)]


def test_test_heartbeat_pings_the_monitor_and_reports_the_answer(tmp_path, monitor, capsys):
    url = f"http://127.0.0.1:{monitor.server_port}/ping/abc"
    assert main(env(tmp_path, HEARTBEAT_URL=url) + ["test-heartbeat"]) == 0
    assert monitor.hits == ["/ping/abc"]
    assert "should go green" in capsys.readouterr().out


def test_test_heartbeat_fails_loudly_when_the_monitor_refuses_or_is_unreachable(tmp_path, monitor, capsys):
    bad = f"http://127.0.0.1:{monitor.server_port}/ping/boom"
    assert main(env(tmp_path, HEARTBEAT_URL=bad) + ["test-heartbeat"]) == 1
    assert "HTTP 500" in capsys.readouterr().err
    assert main(env(tmp_path, HEARTBEAT_URL="http://127.0.0.1:9/ping") + ["test-heartbeat"]) == 1


def test_test_heartbeat_says_so_when_no_monitor_is_configured(tmp_path, capsys):
    assert main(env(tmp_path) + ["test-heartbeat"]) == 2
    assert "HEARTBEAT_URL is not set" in capsys.readouterr().err


def test_a_ping_is_only_good_when_the_monitor_recognises_the_url(monitor):
    base = f"http://127.0.0.1:{monitor.server_port}/ping"
    assert monitor_ping_error(f"{base}/abc") is None
    assert "does not know this ping URL" in monitor_ping_error(f"{base}/unknown")
    assert "HTTP 500" in monitor_ping_error(f"{base}/boom")
    assert "could not be reached" in monitor_ping_error("http://127.0.0.1:9/ping")


def test_test_heartbeat_refuses_a_url_the_monitor_does_not_recognise(tmp_path, monitor, capsys):
    url = f"http://127.0.0.1:{monitor.server_port}/ping/unknown"
    assert main(env(tmp_path, HEARTBEAT_URL=url) + ["test-heartbeat"]) == 1
    out = capsys.readouterr()
    assert "does not know this ping URL" in out.err
    assert "should go green" not in out.out


def test_status_reports_whether_the_outside_monitor_is_working(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    off = Settings(data_dir=data)
    assert _monitor_line(off) == "not set up"

    on = Settings(data_dir=data, heartbeat_url="https://hc.example/ping")
    assert _monitor_line(on) == "on, nothing reported yet"

    off.status_path.write_text(json.dumps({"heartbeat": {"ok_at": "2026-11-09T10:00:00-05:00", "failing": False}}))
    assert _monitor_line(on) == "on, last ping 2026-11-09T10:00:00-05:00"

    off.status_path.write_text(json.dumps({"heartbeat": {"ok_at": "2026-11-09T10:00:00-05:00", "failing": True}}))
    assert _monitor_line(on) == "on, but the last ping failed"

    off.status_path.write_text("not json")
    assert _monitor_line(on) == "on, nothing reported yet"


def test_digest_shows_the_last_seven_days_and_can_email_them(tmp_path, capsys):
    assert main(env(tmp_path) + ["digest"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Elisa: weekly report, ") and "Openings the bot saw: none." in out and "Report sent." not in out
    assert main(env(tmp_path) + ["digest", "--send"]) == 0
    assert "Report sent." in capsys.readouterr().out
