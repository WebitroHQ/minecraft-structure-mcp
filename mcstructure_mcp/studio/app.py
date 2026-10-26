"""Structure Studio — a local web app. Run `minecraft-structure-studio`, open the page, connect a model
(Claude, OpenAI, OpenRouter, Groq, Ollama, LM Studio...) and describe what to build.

Listens on 127.0.0.1 only. The API key is stored in ~/.minecraft-structure-studio/config.json on this computer
and sent only to the provider you chose. Every POST needs the per-launch token embedded in the page, so other
websites cannot drive the studio (or spend your credits) through your browser.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import server
from ..export import to_mcfunction, to_schem
from .agent import Agent

CONFIG_DIR = Path.home() / ".minecraft-structure-studio"
CONFIG = CONFIG_DIR / "config.json"
STATIC = Path(__file__).parent / "static"
DEFAULTS = {"provider": "anthropic", "model": "claude-opus-5-5", "base_url": "", "api_key": "", "effort": "xhigh",
            "vision": True, "max_steps": 300}
TOKEN = secrets.token_urlsafe(24)
AGENT = Agent()
_render_lock = threading.Lock()
_render_cache: dict = {}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if not cfg.get("api_key") and cfg.get("provider") == "anthropic":
        cfg["api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


def public_config(cfg: dict) -> dict:
    out = {k: v for k, v in cfg.items() if k != "api_key"}
    key = cfg.get("api_key") or ""
    out["api_key_set"] = bool(key)
    out["api_key_hint"] = ("…" + key[-4:]) if len(key) > 8 else ""
    return out


def preview_png(view: str, cut_y: int | None) -> bytes | None:
    name = AGENT.current
    if not name or name not in server.STRUCTURES:
        return None
    key = (name, AGENT.version, view, cut_y)
    with _render_lock:
        if _render_cache.get("key") == key:
            return _render_cache["png"]
        s = server.STRUCTURES[name]
        longest = max(s.size)
        scale = max(4, min(24, 1400 // max(1, longest)))
        yaw = {"se": 0, "sw": 90, "nw": 180, "ne": 270}.get(view, 0)
        img = server.renderer().iso(s, scale=scale, cut_y=cut_y, yaw=yaw)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG", optimize=True)
        _render_cache.update(key=key, png=buf.getvalue())
        return _render_cache["png"]


class Handler(BaseHTTPRequestHandler):
    server_version = "StructureStudio"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode())

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__STUDIO_TOKEN__", TOKEN)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if url.path == "/api/state":
            s = server.STRUCTURES.get(AGENT.current or "")
            return self._json({
                "events": AGENT.since(int(q.get("since", 0))),
                "running": AGENT.running, "version": AGENT.version, "structure": AGENT.current,
                "size": list(s.size) if s else None, "blocks": len(s.blocks) if s else 0,
                "regions": sorted(s.regions) if s else [],
                "tokens_in": AGENT.tokens_in, "tokens_out": AGENT.tokens_out,
            })
        if url.path == "/api/config":
            return self._json(public_config(load_config()))
        if url.path == "/api/preview":
            cut = q.get("cut_y")
            png = preview_png(q.get("view", "se"), int(cut) if cut not in (None, "") else None)
            return self._send(200, png, "image/png") if png else self._send(204, b"")
        if url.path == "/api/download":
            return self._download(q.get("fmt", "nbt"))
        self._send(404, b"not found", "text/plain")

    def _download(self, fmt: str):
        name = AGENT.current
        if not name or name not in server.STRUCTURES:
            return self._send(404, b"no structure yet", "text/plain")
        s = server.STRUCTURES[name]
        if fmt == "mcfunction":
            body, ext = to_mcfunction(s).encode(), "mcfunction"
        else:
            raw = io.BytesIO()
            (to_schem(s) if fmt == "schem" else s.to_nbt()).write(raw)
            body, ext = gzip.compress(raw.getvalue()), "schem" if fmt == "schem" else "nbt"
        self._send(200, body, "application/octet-stream",
                   {"Content-Disposition": f'attachment; filename="{name}.{ext}"'})

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        if self.headers.get("X-Studio-Token") != TOKEN:
            return self._json({"error": "bad token"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                cfg = load_config()
                for k in ("provider", "preset", "model", "base_url", "effort", "vision", "max_steps"):
                    if k in body:
                        cfg[k] = body[k]
                if body.get("api_key"):            # empty = keep the stored key
                    cfg["api_key"] = body["api_key"].strip()
                if body.get("clear_key"):
                    cfg["api_key"] = ""
                save_config(cfg)
                return self._json(public_config(cfg))
            if path == "/api/chat":
                text = (body.get("message") or "").strip()
                if not text:
                    return self._json({"error": "empty message"}, 400)
                cfg = load_config()
                if not cfg.get("api_key") and cfg.get("provider") == "anthropic":
                    return self._json({"error": "Add your API key in Settings first."}, 400)
                AGENT.start(text, cfg)
                return self._json({"ok": True})
            if path == "/api/stop":
                AGENT.stop()
                return self._json({"ok": True})
            if path == "/api/reset":
                AGENT.reset()
                return self._json({"ok": True})
            if path == "/api/open":
                msg = server.open_structure(body["path"])
                AGENT.current = Path(body["path"]).stem
                AGENT.version += 1
                return self._json({"ok": True, "message": msg})
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 400)
        self._json({"error": "not found"}, 404)


def main() -> None:
    ap = argparse.ArgumentParser(description="Structure Studio: design Minecraft structures with a model you choose.")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Structure Studio running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
