"""The OpenAI-compatible endpoint side of catalog.py + client.py."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness import catalog  # noqa: E402
from rt_harness.client import OpenAIClient  # noqa: E402
from rt_harness.config import Config  # noqa: E402

OK = 0
FAIL = 0
def check(label, ok, detail=""):
    global OK, FAIL
    if condition := ok:
        OK += 1
        print(f"ok   {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"FAIL {label}" + (f"  {detail}" if detail else ""))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A003 - quiet is the point
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "object": "list",
                "data": [{"id": "gpt-4o-mini"}, {"id": "o1-mini"}],
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            # a simple two-chunk SSE: content then [DONE]
            chunk = lambda text: json.dumps({
                "id": "x",
                "choices": [{"delta": {"content": text}, "index": 0, "finish_reason": None}],
                "object": "chat.completion.chunk",
            })
            self.wfile.write(f"data: {chunk('hello from ')}\n\n".encode())
            self.wfile.write(f"data: {chunk('openai')}\n\n".encode())
            done = json.dumps({
                "id": "x",
                "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}],
                "object": "chat.completion.chunk",
            })
            self.wfile.write(f"data: {done}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_response(404)
            self.end_headers()


server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}/v1"

with TemporaryDirectory() as tmpdir:
    apis_path = catalog._apis_path()
    real_path = apis_path
    catalog._apis_path = lambda: Path(tmpdir) / "apis.json"  # type: ignore[misc]
    try:
        cfg = Config.from_env({})
        cfg.ollama_url = base
        client = OpenAIClient(base)
        models = client.models()
        check("models parsed from /v1/models", models == ["gpt-4o-mini", "o1-mini"], str(models))

        out = iter(client.chat_stream(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            options={},
        ))
        first = next(out)
        second = next(out)
        check("first chunk has a delta", "delta" in first["choices"][0])
        check("the delta carries the text", first["choices"][0]["delta"]["content"] == "hello from ")
        # catalog picks up the endpoint by /v1 suffix
        cfg.ollama_url = base
        check("catalog routes a /v1 endpoint via the OpenAI client", catalog.known_models(cfg) == ["gpt-4o-mini", "o1-mini"])
    finally:
        catalog._apis_path = lambda: real_path  # type: ignore[misc]
server.shutdown()

print(f"{OK} ok, {FAIL} failure(s)")
sys.exit(1 if FAIL else 0)
