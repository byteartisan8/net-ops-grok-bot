"""``grok-bot-sink``: a stand-in for the three roles' Grok Bot routines (tests and dry runs; no cloud, no cost).

It records every page POSTed to ``/<role>`` (``noc-engineer``, ``network-engineer``, ``noc-manager``) and shows them:

* ``POST /<role>``: a page (JSON). Answers 200, or the status set with ``SINK_STATUS`` (e.g. 401 to try refusals);
* ``GET /pages`` (``?role=``, ``?after=<seq>``): the recorded pages, oldest first, each with ``seq``, ``role``,
  ``received``, ``authorization`` (the header as received) and ``body``;
* ``DELETE /pages``: forget them; ``GET /``: a short summary.

grok-bot delivers to it once the operator sets ``sink_url`` (``POST /api/control``; ``make bot-sink-up`` does), with
``Authorization: Bearer test-sink``, never with a routine's real key. Container: ``routine-sink`` at 172.30.10.61:8096,
host 127.0.0.1:8096 (``bot/compose.yaml``, profile ``sink``).
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROLES = ("noc-engineer", "network-engineer", "noc-manager")


class Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pages: list[dict[str, Any]] = []
        self.seq = 0

    def add(self, role: str, auth: str | None, body: Any) -> dict[str, Any]:
        with self.lock:
            self.seq += 1
            rec = {
                "seq": self.seq,
                "role": role,
                "received": round(time.time(), 3),
                "authorization": auth,
                "body": body,
            }
            self.pages.append(rec)
            del self.pages[:-2000]
            return rec

    def list(self, role: str | None = None, after: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            return [p for p in self.pages if p["seq"] > after and (role is None or p["role"] == role)]

    def clear(self) -> None:
        with self.lock:
            self.pages.clear()


def make_handler(rec: Recorder, status: int) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, data: Any) -> None:
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            role = urlsplit(self.path).path.strip("/")
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            if role not in ROLES:
                self._send(404, {"error": f"POST /<role>: one of {', '.join(ROLES)}"})
                return
            try:
                body: Any = json.loads(raw)
            except ValueError:
                body = raw.decode("utf-8", "replace")
            r = rec.add(role, self.headers.get("Authorization"), body)
            self._send(status, {"accepted": status < 300, "seq": r["seq"]})

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            if url.path == "/pages":
                role = (q.get("role") or [None])[0]
                after = int((q.get("after") or ["0"])[0] or 0)
                self._send(200, rec.list(role, after))
            elif url.path in ("/", "/health"):
                pages = rec.list()
                self._send(200, {"pages": len(pages), "by_role": {r: len(rec.list(r)) for r in ROLES}})
            else:
                self._send(404, {"error": "GET /pages or /"})

        def do_DELETE(self) -> None:
            if urlsplit(self.path).path == "/pages":
                rec.clear()
                self._send(200, {"cleared": True})
            else:
                self._send(404, {"error": "DELETE /pages"})

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grok-bot-sink", description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=os.environ.get("SINK_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SINK_PORT", "8096")))
    args = parser.parse_args(argv)
    status = int(os.environ.get("SINK_STATUS") or 200)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(Recorder(), status))
    print(f"grok-bot-sink: recording pages on {args.host}:{args.port} (answers {status})", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
