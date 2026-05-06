"""Pineapple plugin shim for the Blackmagic service.

Copy this file into pineapplediscoverypipeline/scripts/BlackmagicInterface.py.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import websockets

_send_response = None
_cfg: dict[str, Any] | None = None
_loop: asyncio.AbstractEventLoop | None = None


def init(send_response_fn, config):
    global _send_response, _cfg, _loop
    _send_response = send_response_fn
    _cfg = config
    _loop = asyncio.new_event_loop()
    threading.Thread(target=_loop.run_forever, daemon=True).start()


def handle_message(cmd: dict):
    if cmd.get("type") not in ("recordStart", "recordStop", "fileName", "health"):
        return
    if not cmd.get("ip") or not cmd.get("port") or _loop is None:
        return
    asyncio.run_coroutine_threadsafe(_send_to_blackmagic(cmd), _loop)


async def _send_to_blackmagic(cmd: dict):
    device = (_cfg or {}).get("attached_name", "Blackmagic Camera")
    uri = f"ws://{cmd['ip']}:{cmd['port']}"
    payload = _build_payload(cmd)
    if payload is None:
        return

    try:
        async with websockets.connect(uri) as ws:
            await ws.send(payload)
            reply = await asyncio.wait_for(ws.recv(), timeout=5)
            if cmd["type"] == "health":
                _respond({"type": "health_response", "device": device, "value": reply == "Good", "msg": reply})
                return
            try:
                obj = json.loads(reply)
            except json.JSONDecodeError:
                obj = {"type": "status", "msg": reply}
            if isinstance(obj, dict):
                _respond(obj)
    except Exception as exc:
        if cmd["type"] == "health":
            _respond({"type": "health_response", "device": device, "value": False})
        print(f"[BlackmagicInterface] Error talking to {uri}: {exc}")


def _build_payload(cmd: dict) -> str | None:
    typ = cmd["type"]
    if typ == "recordStart":
        return "Start"
    if typ == "recordStop":
        return "Stop"
    if typ == "fileName":
        return f"SetName {cmd.get('value', '')}"
    if typ == "health":
        return "health"
    return None


def _respond(msg: dict) -> None:
    if _send_response is not None:
        _send_response(msg)

