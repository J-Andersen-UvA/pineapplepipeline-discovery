import asyncio, threading, websockets

_send_response = None
_cfg           = None
_loop          = None

def init(send_response_fn, config):
    """
    Called once by PluginManager.
      - send_response_fn: use this to call back into PineappleListener
      - config: that device’s YAML dict (hostname, attached_name, etc)
    """
    global _send_response, _cfg, _loop
    _send_response = send_response_fn
    _cfg           = config

    # Create a dedicated asyncio loop for outgoing WS calls
    _loop = asyncio.new_event_loop()
    t = threading.Thread(target=_loop.run_forever, daemon=True)
    t.start()

def handle_message(cmd: dict):
    """
    Called on *every* command from DiscoveryService.
    We only react to the types we care about.
    """
    ctype = cmd.get("type")
    if ctype not in ("recordStart", "recordStop", "broadcastGlos", "fileName", "health"):
        return

    ip   = cmd.get("ip")
    port = cmd.get("port")
    if not ip or not port:
        # Not resolved yet
        return

    # Schedule the coroutine on our dedicated loop
    if _loop is not None:
        asyncio.run_coroutine_threadsafe(_send_to_llf(cmd), _loop)

async def _send_to_llf(cmd : dict):
    """
    Connect → send → (maybe receive health reply) → report back → close.
    """
    if _cfg is None or "attached_name" not in _cfg:
        print("[LLF interface] Error: _cfg is not initialized or missing 'attached_name'")
        return
    device = _cfg["attached_name"]
    uri    = f"ws://{cmd['ip']}:{cmd['port']}"
    payload = _build_payload(cmd)
    if payload is None:
        return
    elif payload == "health":
        # In addition to the health check, we will also send over the sub_ip
        sub_ip = cmd.get("sub_ip", None)
        if sub_ip:
            payload += f" {sub_ip}"
        # If we are missing the sub_ip, we will respond immediately with False
        else:
            if _send_response is not None:
                _send_response({
                    "type":   "health_response",
                    "device": device,
                    "value":  False
                })
            return

    try:
        async with websockets.connect(uri) as ws:
            # send the command payload
            await ws.send(payload)

            # if it's a health check, block until the one reply
            if cmd["type"] == "health":
                reply = await asyncio.wait_for(ws.recv(), timeout=5)
                ok = (reply == "Good")
                if _send_response is not None:
                    # send the health response back to PineappleListener
                    _send_response({
                        "type":   "health_response",
                        "device": device,
                        "value":  ok,
                        "msg":    reply,
                    })

    except Exception as e:
        # on any error, report failure for health
        if cmd["type"] == "health":
            if _send_response is not None:
                _send_response({
                    "type":   "health_response",
                    "device": device,
                    "value":  False
                })
        print(f"[LLF interface] Error talking to {uri}: {e}")

def _build_payload(cmd: dict) -> str | None:
    """
    Map PineappleListener cmd → the single‐line string your OBS WS wants.
    Return None to skip sending.
    """
    t = cmd["type"]
    if t == "recordStart":
        return "Start"
    if t == "recordStop":
        return "Stop"
    if t in ("broadcastGlos", "fileName"):
        return f"SetName {cmd.get('value','')}"
    if t == "health":
        return "health"
    return None
