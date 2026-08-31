"""
dashboard_server.py - Two cooperating servers, kept simple & dependency-free:

  * HTTP server (stdlib http.server) serves dashboard.html on HTTP_PORT.
  * WebSocket server (the already-installed `websockets` pkg) streams
    telemetry events on WS_PORT.

The browser opens the HTTP URL; the page auto-connects its WebSocket to
WS_PORT (passed into the HTML as a variable). One URL for the user, clean
separation so we never depend on websockets' fragile process_request API.

Run via `python run_dashboard.py` (which imports this). Default:
    http://localhost:8765   (HTML)
    ws://localhost:8766     (telemetry stream)
"""
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.server import serve

from telemetry import tel

HTML_PATH = pathlib.Path(__file__).with_name("dashboard.html")
HTTP_PORT = 8765
WS_PORT = 8766


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/dashboard"):
            try:
                html = HTML_PATH.read_text(encoding="utf-8")
            except Exception:
                html = "<h1>dashboard.html not found</h1>"
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")

    def log_message(self, *args):
        pass  # quiet


async def _ws_handler(websocket):
    tel.subscribers.add(websocket)
    try:
        await websocket.send(json.dumps(tel.snapshot(), default=str))
        async for message in websocket:
            try:
                cmd = json.loads(message)
            except Exception:
                continue
            action = (cmd.get("cmd") or "").lower()
            if action == "pause":
                await tel.set_pause(True)
            elif action == "resume":
                await tel.set_pause(False)
            elif action == "clear_feed":
                tel.feed.clear()
    except Exception:
        pass
    finally:
        tel.subscribers.discard(websocket)


def start_http():
    httpd = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


async def serve_dashboard(host: str = "0.0.0.0", port: int = WS_PORT):
    # HTTP server runs in a background thread (stdlib, no asyncio needed).
    httpd = start_http()
    # WebSocket server runs on the asyncio loop.
    ws_server = await serve(_ws_handler, host, port)
    return ws_server, httpd
