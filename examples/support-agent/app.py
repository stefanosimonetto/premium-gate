"""Larkspur Mobile support agent: a minimal chat service (standard library only).

    ANTHROPIC_API_KEY=... python app.py      # http://localhost:8080

POST /chat {"message": "..."} -> {"reply": "..."}
The system prompt and policy are read from agent/, the same files premium-gate
tests and hashes, so what is priced is what is deployed.
"""
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
MODEL = os.environ.get("AGENT_MODEL", "claude-haiku-5-5")
SYSTEM = (ROOT / "agent/system_prompt.md").read_text() + "\n\n[Policy document]\n" + (ROOT / "agent/policy.md").read_text()

PAGE = """<!doctype html><meta name=viewport content="width=device-width"><title>Larkspur support</title>
<body style="font:16px system-ui;max-width:640px;margin:40px auto;padding:0 16px">
<h1>Larkspur Mobile support</h1><div id=log></div>
<form id=f><input id=m style="width:80%;padding:8px" placeholder="Ask about your plan"><button>Send</button></form>
<script>f.onsubmit=async e=>{e.preventDefault();const t=m.value;m.value='';
const p=document.createElement('p');p.textContent='You: '+t;log.append(p);
const r=await fetch('/chat',{method:'POST',body:JSON.stringify({message:t})});const j=await r.json();
const q=document.createElement('p');q.textContent='Agent: '+(j.reply||j.error);log.append(q)}</script>"""


def ask(message: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Set ANTHROPIC_API_KEY to run the agent.")
    body = {"model": MODEL, "max_tokens": 400, "system": SYSTEM, "messages": [{"role": "user", "content": message}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, '{"ok": true}')
        self._send(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/chat":
            return self._send(404, '{"error": "not found"}')
        try:
            msg = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))["message"]
            self._send(200, json.dumps({"reply": ask(msg)}))
        except Exception as e:
            self._send(503, json.dumps({"error": str(e)}))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Larkspur support agent on :{port} (model {MODEL})")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
