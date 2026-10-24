"""Offline SSE parsing and a real incremental loopback connection."""
from __future__ import annotations

import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'paperdaily').is_dir())
sys.path.insert(0, str(ROOT / '_shared' if (ROOT / '_shared').is_dir() else ROOT / 'paperdaily/lib'))
from pd_runtime import ApiError, Client, Settings, read_sse


class Response(io.BytesIO):
    headers = {'Content-Type': 'text/event-stream; charset=utf-8'}


def test_restart_and_utf8_final_correction() -> None:
    data = (': heartbeat\r\n\r\nevent: token\r\ndata: {"delta":"旧答案"}\r\n\r\n'
            'event: restart\ndata: {"attempt":1}\n\n'
            'event: done\ndata: {"answer":"校准答案\\n第二行",\ndata: "cited_paper_ids":["W1"]}\n\n')
    seen = []
    result = read_sse(Response(data.encode()), lambda name, body: seen.append((name, body)))
    assert [name for name, _ in seen] == ['token', 'restart', 'done']
    assert result['answer'] == '校准答案\n第二行'
    assert result['cited_paper_ids'] == ['W1']


@pytest.mark.parametrize('body', [
    b'event: token\ndata: {"delta":"partial"}\n\n',
    b'event: done\ndata: {"answer":"missing refs"}\n\n',
    b'event: error\ndata: {"error":"failed"}\n\n',
    b'event: done\ndata: []\n\n', b'event: token\ndata: broken\n\n',
    b'event: done\ndata: {"answer":"truncated","cited_paper_ids":[]}',
])
def test_incomplete_or_error_never_becomes_success(body: bytes) -> None:
    with pytest.raises(ApiError):
        read_sse(Response(body))


def test_client_reads_progress_before_completion_without_retry() -> None:
    release, observed = threading.Event(), threading.Event()
    calls = []
    results, errors = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            assert self.headers['Accept'] == 'text/event-stream'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'event: progress\ndata: {"stage":"retrieving"}\n\n')
            self.wfile.flush()
            release.wait(5)
            self.wfile.write(b'event: done\ndata: {"answer":"final","cited_paper_ids":[]}\n\n')
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    client = Client(Settings(f'http://127.0.0.1:{server.server_port}', 'fixture-only', throttle=0))

    def run():
        try:
            results.append(client.request('POST', '/ask?stream=true', {'question': 'x'},
                on_event=lambda name, body: observed.set() if name == 'progress' else None))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert observed.wait(3), 'progress must arrive while generation is blocked'
        assert not results
    finally:
        release.set()
        worker.join(5)
        server.shutdown()
        server.server_close()
        serving.join(5)
    assert not errors
    assert results[0]['answer'] == 'final'
    assert calls == ['/ask?stream=true']


def test_chunked_transport_failure_is_not_a_success() -> None:
    import http.client
    class Broken:
        headers = {'Content-Type': 'text/event-stream'}
        def readline(self, limit):
            raise http.client.IncompleteRead(b'partial')
    with pytest.raises(ApiError, match='outcome unknown'):
        read_sse(Broken())
