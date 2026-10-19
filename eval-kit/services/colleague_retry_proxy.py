#!/usr/bin/env python3
"""Host-side colleague Responses proxy that retries stalled upstream streams.

The upstream occasionally stops sending bytes mid-stream; the in-container
client then waits out its whole decision budget. This proxy reads each
upstream attempt with an idle timeout, buffers the full SSE body, and only
returns to the caller once a stream reached response.completed (or [DONE]).
Retryable failures (idle stall, network error, 429/5xx, failed/incomplete
stream) are retried with backoff. Request/response bodies are never logged.
"""
import argparse
import http.client
import http.server
import json
import socket
import time
import urllib.error
import urllib.request

RETRYABLE_HTTP = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}


def log(server, **fields):
    fields["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(server.log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(fields) + "\n")


def attempt(server, path, body, headers):
    """Return (status, content_type, payload_bytes, outcome)."""
    request = urllib.request.Request(server.upstream + path, data=body, headers=headers, method="POST")
    started = time.monotonic()
    try:
        response = urllib.request.urlopen(request, timeout=server.connect_timeout)
    except urllib.error.HTTPError as error:
        payload = error.read()
        error.close()
        return error.code, error.headers.get("Content-Type", "application/json"), payload, f"http_{error.code}"
    except (urllib.error.URLError, OSError, socket.timeout):
        return None, None, None, "connect_error"
    with response:
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        chunks = []
        while True:
            if time.monotonic() - started > server.attempt_timeout:
                return None, None, None, "attempt_deadline"
            if sock is not None:
                sock.settimeout(server.idle_timeout)
            try:
                chunk = response.read1(65536) if hasattr(response, "read1") else response.read(65536)
            except (socket.timeout, TimeoutError):
                return None, None, None, "idle_stall"
            except (OSError, http.client.HTTPException):
                return None, None, None, "read_error"
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
    if "event-stream" in content_type:
        if b"response.failed" in payload or b"response.incomplete" in payload or b'"type":"error"' in payload:
            return None, None, None, "stream_failed"
        if b"response.completed" not in payload and b"[DONE]" not in payload:
            return None, None, None, "stream_truncated"
    return response.status, content_type, payload, "ok"


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        return

    def reply(self, status, content_type, payload):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/healthz":
            self.reply(200, "application/json", b'{"ok":true}')
        else:
            self.reply(404, "application/json", b'{"error":"not found"}')

    def do_POST(self):
        server = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        path = self.path[3:] if self.path.startswith("/v1/") else self.path
        headers = {
            "Authorization": "Bearer " + server.key,
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept": self.headers.get("Accept", "text/event-stream"),
        }
        try:
            model = json.loads(body).get("model")
        except Exception:
            model = None
        started = time.monotonic()
        outcomes = []
        for number in range(1, server.max_attempts + 1):
            status, content_type, payload, outcome = attempt(server, path, body, headers)
            outcomes.append(outcome)
            if outcome == "ok" or (status is not None and status not in RETRYABLE_HTTP):
                log(server, model=model, bytes_in=len(body), attempts=number, outcomes=outcomes,
                    status=status, seconds=round(time.monotonic() - started, 1))
                try:
                    self.reply(status, content_type, payload)
                except OSError:
                    pass
                return
            time.sleep(min(30, 2 * number))
        log(server, model=model, bytes_in=len(body), attempts=server.max_attempts, outcomes=outcomes,
            status=502, seconds=round(time.monotonic() - started, 1))
        try:
            self.reply(502, "application/json", b'{"error":{"message":"upstream retries exhausted"}}')
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="0.0.0.0:18090")
    parser.add_argument("--upstream", required=True, help="base URL including /v1")
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--idle-timeout", type=float, default=60)
    parser.add_argument("--attempt-timeout", type=float, default=240)
    parser.add_argument("--connect-timeout", type=float, default=60)
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--log", default="/var/log/colleague-retry-proxy.jsonl")
    args = parser.parse_args()
    host, port = args.listen.rsplit(":", 1)
    server = http.server.ThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    server.upstream = args.upstream.rstrip("/")
    server.key = open(args.key_file, encoding="utf-8").read().strip()
    server.idle_timeout = args.idle_timeout
    server.attempt_timeout = args.attempt_timeout
    server.connect_timeout = args.connect_timeout
    server.max_attempts = args.max_attempts
    server.log_path = args.log
    print(json.dumps({"listen": args.listen, "upstream": server.upstream}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    import http.client  # noqa: F401  (used in exception tuple)
    main()
