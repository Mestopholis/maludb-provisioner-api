"""Serve the static frontend and proxy /api to the control plane.

This avoids adding a frontend build tool and avoids requiring CORS middleware in
the FastAPI app just to run the console during development.
"""

from __future__ import annotations

import argparse
import http.client
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class FrontendHandler(BaseHTTPRequestHandler):
    api_target: str

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        message = format % args
        safe = message.replace("Authorization", "authorization")
        print(f"{self.address_string()} - {safe}")

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self.proxy()
            return
        self.serve_static()

    def do_POST(self) -> None:
        self.proxy()

    def do_PUT(self) -> None:
        self.proxy()

    def do_DELETE(self) -> None:
        self.proxy()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def serve_static(self) -> None:
        parsed = urlparse(self.path)
        relative = parsed.path.lstrip("/") or "index.html"
        candidate = (ROOT / relative).resolve()
        if not candidate.is_file() or ROOT not in candidate.parents:
            self.send_error(404)
            return
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def proxy(self) -> None:
        target = urlparse(self.api_target)
        upstream_path = self.path.removeprefix("/api")
        if not upstream_path.startswith("/"):
            upstream_path = "/" + upstream_path
        if target.query:
            upstream_path = f"{upstream_path}?{target.query}"

        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS and name.lower() != "host"
        }
        headers["Host"] = target.netloc

        connection_class = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        port = target.port or (443 if target.scheme == "https" else 80)
        connection = connection_class(target.hostname, port, timeout=30)
        try:
            connection.request(self.command, upstream_path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                if name.lower() not in HOP_BY_HOP_HEADERS:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError as exc:
            self.send_error(502, f"control plane unavailable: {exc}")
        finally:
            connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the MaluDB static frontend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=5173, type=int)
    parser.add_argument(
        "--api",
        default=os.environ.get("MALUDB_FRONTEND_API", "http://127.0.0.1:8112"),
        help="control-plane API origin to proxy under /api",
    )
    args = parser.parse_args()

    FrontendHandler.api_target = args.api.rstrip("/")
    server = ThreadingHTTPServer((args.host, args.port), FrontendHandler)
    print(f"frontend: http://{args.host}:{args.port}")
    print(f"proxy: /api -> {FrontendHandler.api_target}")
    server.serve_forever()


if __name__ == "__main__":
    main()
