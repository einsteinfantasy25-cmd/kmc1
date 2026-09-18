import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("kmc_health")
_server = None
_lock = threading.Lock()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            body = b"KMC bot is running"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        return


def start_health_server():
    global _server
    with _lock:
        if _server is not None:
            return _server
        port = int(os.environ.get("PORT", "10000"))
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
        thread.start()
        _server = server
        logger.info("Health server listening on 0.0.0.0:%s", port)
        return server
