import os import threading from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class HealthHandler(BaseHTTPRequestHandler): def do_GET(self): if self.path in ("/", "/health"): self.send_response(200) self.send_header("Content-Type", "text/plain; charset=utf-8") self.end_headers() self.wfile.write(b"Bot is running") else: self.send_response(404) self.end_headers()
def log_message(self, format, *args):
    pass
def start_health_server(): port = int(os.environ.get("PORT", "10000"))
server = ThreadingHTTPServer(
    ("0.0.0.0", port),
    HealthHandler
)

thread = threading.Thread(
    target=server.serve_forever,
    daemon=True
)

thread.start()
print(f"Health server running on port {port}")
