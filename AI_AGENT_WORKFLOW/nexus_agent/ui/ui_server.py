# ui/server.py
"""
Nexus Agent — Web UI Server
════════════════════════════
FastAPI + WebSocket bridge between the browser and the agent.

Architecture:
  Browser ──WebSocket──► server.py ──async generator──► Agent ──Tools──► OS

The browser sends JSON messages:
  { "type": "chat",    "content": "..." }
  { "type": "confirm", "approved": true/false }
  { "type": "interrupt" }
  { "type": "set_model", "model": "..." }
  { "type": "set_cwd",   "cwd": "..." }

The server streams JSON back:
  { "type": "text_delta",     "content": "..." }
  { "type": "text_complete",  "content": "..." }
  { "type": "tool_start",     "call_id", "name", "kind", "args" }
  { "type": "tool_complete",  "call_id", "name", "kind", "success",
                               "output", "error", "diff", "metadata" }
  { "type": "confirm_request","call_id", "tool_name", "description",
                               "command", "diff", "is_dangerous" }
  { "type": "agent_error",    "error": "..." }
  { "type": "agent_end" }
  { "type": "system_info",    "cwd", "model", "tools" }
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ── locate the agent package ─────────────────────────────────────────────────
_HERE = Path(__file__).parent.resolve()
# Walk up until we find the agent root (contains "Agent/" and "Tools/")
_ROOT = _HERE
for _candidate in [_HERE, _HERE.parent, _HERE.parent.parent]:
    if (_candidate / "Agent").is_dir() and (_candidate / "Tools").is_dir():
        _ROOT = _candidate
        break

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from nexus_agent.Agent.agent import Agent
from nexus_agent.Agent.events import AgentEventType
from nexus_agent.config.loader import load_config
from nexus_agent.Tools.base import ToolConfirmation

# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Nexus Agent UI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── serve the single-page frontend inline ────────────────────────────────────
_UI_HTML_PATH = Path(__file__).parent / "nexus_ui.html"


@app.get("/", response_class=HTMLResponse)
async def root():
    if _UI_HTML_PATH.exists():
        return HTMLResponse(_UI_HTML_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>UI file not found. Place nexus_ui.html next to server.py</h1>")


# ── File browser + download endpoints ────────────────────────────────────────
import zipfile, mimetypes
from fastapi import Query
from fastapi.responses import FileResponse, StreamingResponse

def _check_token(token: str = "") -> bool:
    """Validate the download token (same secret as WebSocket)."""
    required = os.environ.get("UI_SECRET", "")
    return not required or token == required

@app.get("/files")
async def list_files(
    path:  str   = Query(".", description="Directory path relative to project root"),
    token: str   = Query("",  description="UI_SECRET token"),
):
    """Return a JSON list of files/dirs at the given path."""
    if not _check_token(token):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")

    base = Path.cwd()
    target = (base / path).resolve()

    # Safety: never escape the project root
    if not str(target).startswith(str(base)):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Access denied")

    if not target.exists():
        return {"error": f"Path not found: {path}", "items": []}

    items = []
    if target.is_file():
        items.append({
            "name": target.name,
            "path": str(target.relative_to(base)),
            "type": "file",
            "size": target.stat().st_size,
        })
    else:
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
            items.append({
                "name":  item.name,
                "path":  str(item.relative_to(base)),
                "type":  "file" if item.is_file() else "dir",
                "size":  item.stat().st_size if item.is_file() else None,
            })

    return {"cwd": str(base), "path": str(target.relative_to(base)), "items": items}


@app.get("/download")
async def download_file(
    path:  str = Query(..., description="File or directory path relative to project root"),
    token: str = Query("",  description="UI_SECRET token"),
):
    """
    Download a single file, or a directory as a .zip archive.
    Example: GET /download?path=my_project&token=SECRET
    """
    if not _check_token(token):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Unauthorized")

    base   = Path.cwd()
    target = (base / path).resolve()

    if not str(target).startswith(str(base)):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Access denied")

    if not target.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Not found: {path}")

    # Single file → serve directly
    if target.is_file():
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(
            path=str(target),
            filename=target.name,
            media_type=media_type,
        )

    # Directory → zip it on the fly and stream to browser
    def _zip_generator():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in target.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(target))
        buf.seek(0)
        yield buf.read()

    return StreamingResponse(
        _zip_generator(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{target.name}.zip"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# PER-CONNECTION STATE
# ─────────────────────────────────────────────────────────────────────────────

class Connection:
    """Holds all state for one browser tab / WebSocket connection."""

    def __init__(self, ws: WebSocket):
        self.ws           = ws
        self.config       = load_config(cwd=Path.cwd())
        self._agent: Agent | None = None
        self._confirm_evt = asyncio.Event()
        self._confirm_ans: bool = True
        self._interrupt   = False

    # ── outbound helpers ─────────────────────────────────────────────────────

    async def send(self, msg: dict):
        try:
            await self.ws.send_text(json.dumps(msg, default=str))
        except Exception:
            pass

    # ── confirmation callback (called from agent thread) ─────────────────────

    def _sync_confirm(self, confirmation: ToolConfirmation) -> bool:
        """
        Bridge: agent calls this synchronously; we need to suspend it
        and wait for the browser's answer via an asyncio Event.
        """
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self._ask_confirm(confirmation))

    async def _ask_confirm(self, conf: ToolConfirmation) -> bool:
        self._confirm_evt.clear()
        await self.send({
            "type":        "confirm_request",
            "tool_name":   conf.tool_name,
            "description": conf.description,
            "command":     conf.command,
            "diff":        conf.diff.create_diff() if conf.diff else None,
            "is_dangerous": conf.is_dangerous,
        })
        await self._confirm_evt.wait()
        return self._confirm_ans

    # ── system info ──────────────────────────────────────────────────────────

    async def send_system_info(self):
        # dynamically load tool list
        try:
            from Tools.builtin import get_all_builtin_tools
            tool_names = [t.name for t in get_all_builtin_tools()]
        except Exception:
            tool_names = []

        await self.send({
            "type":  "system_info",
            "cwd":   str(self.config.cwd),
            "model": self.config.model_name,
            "tools": tool_names,
        })

    # ── run one agent turn ───────────────────────────────────────────────────

    async def run_message(self, content: str):
        self._interrupt = False

        async with Agent(
            self.config,
            confirmation_callback=self._sync_confirm,
        ) as agent:
            self._agent = agent
            async for event in agent.run(content):
                if self._interrupt:
                    break

                t = event.type

                if t == AgentEventType.TEXT_DELTA:
                    await self.send({
                        "type":    "text_delta",
                        "content": event.data.get("content", ""),
                    })

                elif t == AgentEventType.TEXT_COMPLETE:
                    await self.send({
                        "type":    "text_complete",
                        "content": event.data.get("content", ""),
                    })

                elif t == AgentEventType.TOOL_CALL_START:
                    # get kind from registry
                    kind = None
                    try:
                        tool = agent.session.tool_registry.get(event.data["name"])
                        if tool:
                            kind = tool.kind.value
                    except Exception:
                        pass
                    await self.send({
                        "type":    "tool_start",
                        "call_id": event.data.get("call_id", ""),
                        "name":    event.data.get("name", ""),
                        "kind":    kind,
                        "args":    event.data.get("arguments", {}),
                    })

                elif t == AgentEventType.TOOL_CALL_COMPLETE:
                    await self.send({
                        "type":     "tool_complete",
                        "call_id":  event.data.get("call_id", ""),
                        "name":     event.data.get("name", ""),
                        "success":  event.data.get("success", False),
                        "output":   (event.data.get("output", "") or "")[:4000],
                        "error":    event.data.get("error"),
                        "diff":     event.data.get("diff"),
                        "metadata": event.data.get("metadata", {}),
                        "truncated":event.data.get("truncated", False),
                        "exit_code":event.data.get("exit_code"),
                    })

                elif t == AgentEventType.AGENT_ERROR:
                    await self.send({
                        "type":  "agent_error",
                        "error": event.data.get("error", "Unknown error"),
                    })

                elif t == AgentEventType.AGENT_END:
                    await self.send({"type": "agent_end"})

            self._agent = None


# ─────────────────────────────────────────────────────────────────────────────
# WEBSOCKET ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    # ── Token auth (only enforced when UI_SECRET env var is set) ─────────────
    # On Render: set  UI_SECRET=some-long-random-string  in Environment Variables
    # Users connect to:  wss://your-app.onrender.com/ws?token=some-long-random-string
    required_token = os.environ.get("UI_SECRET", "")
    if required_token:
        provided_token = ws.query_params.get("token", "")
        if provided_token != required_token:
            await ws.send_text(json.dumps({
                "type":  "agent_error",
                "error": "❌ Unauthorized — invalid or missing token. "
                         "Add ?token=YOUR_SECRET to the WebSocket URL in Settings."
            }))
            await ws.close(code=4001, reason="Unauthorized")
            return

    conn = Connection(ws)
    await conn.send_system_info()

    agent_task: asyncio.Task | None = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            kind = msg.get("type")

            # ── chat message ─────────────────────────────────────────────────
            if kind == "chat":
                content = msg.get("content", "").strip()
                if not content:
                    continue
                # cancel previous task if still running
                if agent_task and not agent_task.done():
                    agent_task.cancel()
                agent_task = asyncio.create_task(conn.run_message(content))

            # ── confirmation answer ──────────────────────────────────────────
            elif kind == "confirm":
                conn._confirm_ans = bool(msg.get("approved", True))
                conn._confirm_evt.set()

            # ── interrupt ────────────────────────────────────────────────────
            elif kind == "interrupt":
                conn._interrupt = True
                if agent_task and not agent_task.done():
                    agent_task.cancel()
                await conn.send({"type": "agent_end"})

            # ── change model ─────────────────────────────────────────────────
            elif kind == "set_model":
                model = msg.get("model", "").strip()
                if model:
                    conn.config.model_name = model
                    await conn.send({"type": "system_info",
                                     "cwd": str(conn.config.cwd),
                                     "model": conn.config.model_name})

            # ── change cwd ───────────────────────────────────────────────────
            elif kind == "set_cwd":
                cwd = Path(msg.get("cwd", ".")).expanduser().resolve()
                if cwd.is_dir():
                    conn.config = load_config(cwd=cwd)
                    await conn.send_system_info()
                else:
                    await conn.send({"type": "agent_error",
                                     "error": f"Directory not found: {cwd}"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await conn.send({"type": "agent_error", "error": str(e)})
        except Exception:
            pass
    finally:
        if agent_task and not agent_task.done():
            agent_task.cancel()


# ─────────────────────────────────────────────────────────────────────────────
# LAUNCH
# ─────────────────────────────────────────────────────────────────────────────

def launch(host: str = "127.0.0.1", port: int = 7860, open_browser: bool = True):
    if open_browser:
        import threading, webbrowser
        def _open():
            import time; time.sleep(1.2)
            webbrowser.open(f"http://{host}:{port}")
        threading.Thread(target=_open, daemon=True).start()

    print(f"\n🌐  Nexus Agent UI  →  http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


def _cli():
    """Entry point for both  python ui/server.py  and  python -m ui.server"""
    import argparse
    ap = argparse.ArgumentParser(description="Nexus Agent Web UI Server")
    ap.add_argument("--host",       default=os.environ.get("HOST", "127.0.0.1"),
                    help="Bind host (default: 127.0.0.1, Render sets 0.0.0.0)")
    ap.add_argument("--port",       type=int,
                    default=int(os.environ.get("PORT", 7860)),
                    help="Bind port (default: 7860, Render sets $PORT automatically)")
    ap.add_argument("--no-browser", action="store_true",
                    help="Do not open a browser tab on startup")
    args = ap.parse_args()
    launch(args.host, args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    _cli()