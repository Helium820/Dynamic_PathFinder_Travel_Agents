"""Phone-sized web app in front of the Japan day agent."""

from __future__ import annotations

import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from planner import plan

WEB = Path(__file__).resolve().parent / "web"
PORT = 8765


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/plan":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            raw = json.loads(self.rfile.read(length) or b"{}")
            result = plan(raw)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        except json.JSONDecodeError:
            self._send(400, {"error": "Request body must be JSON."})
            return
        self._send(200, result)

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print("%s %s" % (self.address_string(), fmt % args))


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Japan day agent at http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
