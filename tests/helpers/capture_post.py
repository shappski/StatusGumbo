"""One-shot HTTP listener used by the statusline reporter tests.

Usage: capture_post.py <port|/socket/path> <outfile> [<authfile>]
Serves a single request, writes its body to outfile, exits 0. Exits 1 if no
request arrives within 10 seconds. With authfile, also writes the request's
Authorization header there (empty when it had none).
"""

import socketserver
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class UnixHTTPServer(socketserver.UnixStreamServer):
    def get_request(self):
        request, _ = super().get_request()
        # BaseHTTPRequestHandler expects a (host, port) client address.
        return request, ("local", 0)


def main():
    where = sys.argv[1]
    outfile = sys.argv[2]
    authfile = sys.argv[3] if len(sys.argv) > 3 else None

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            with open(outfile, "wb") as handle:
                handle.write(body)
            if authfile:
                with open(authfile, "w") as handle:
                    handle.write(self.headers.get("Authorization") or "")
            self.send_response(204)
            self.end_headers()

        def log_message(self, fmt, *args):
            pass

    if where.startswith("/"):
        server = UnixHTTPServer(where, Handler)
    else:
        server = HTTPServer(("127.0.0.1", int(where)), Handler)
    server.timeout = 10
    server.handle_request()
    return 0


if __name__ == "__main__":
    sys.exit(main())
