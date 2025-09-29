# scripts/OBSinterface.py
import asyncio, threading, websockets, json

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
    if ctype not in ("recordStart", "recordStop", "broadcastGlos", "fileName", "health", "setPath"):
        return

    ip   = cmd.get("ip")
    port = cmd.get("port")
    if not ip or not port:
        # Not resolved yet
        return

    # Schedule the coroutine on our dedicated loop
    if _loop is not None:
        asyncio.run_coroutine_threadsafe(_send_to_obs(cmd), _loop)
    else:
        print("[OBSInterface] Error: Event loop is not initialized.")

async def _send_to_obs(cmd: dict):
    """
    Connect → send → (maybe receive health reply) → report back → close.
    """
    if _cfg is None or "attached_name" not in _cfg:
        print("[OBSInterface] Error: _cfg is not initialized or missing 'attached_name'")
        return

    device = _cfg["attached_name"]
    uri    = f"ws://{cmd['ip']}:{cmd['port']}"
    payload = _build_payload(cmd)
    if payload is None:
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
                    _send_response({
                        "type":   "health_response",
                        "device": device,
                        "value":  ok,
                        "msg":    reply
                })
                return
            
            await _pump_incoming(ws, device)

    except Exception as e:
        # on any error, report failure for health
        if cmd["type"] == "health":
            if _send_response is not None:
                _send_response({
                    "type":   "health_response",
                    "device": device,
                    "value":  False
                })
        print(f"[OBSInterface] Error talking to {uri}: {e}")

async def _pump_incoming(ws, device: str, idle_timeout: float = 5.0, overall_cap: float = 30.0):
    """
    Read frames until we've been idle for `idle_timeout` seconds or hit `overall_cap`.
    Any JSON object received is forwarded into the pipeline via _send_response.
    """
    started = asyncio.get_event_loop().time()
    last_rx = started
    while True:
        now = asyncio.get_event_loop().time()
        if (now - last_rx) > idle_timeout or (now - started) > overall_cap:
            break
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout=idle_timeout)
            last_rx = asyncio.get_event_loop().time()

            # Try JSON → forward; otherwise ignore (legacy string replies)
            try:
                obj = json.loads(frame)
                if isinstance(obj, dict):
                    if _send_response is not None:
                        _send_response(obj)  # PluginManager re-adds {"device": <name>, ...}
                else:
                    print("[OBSInterface] Ignoring non-dict WS frame:", obj)
            except json.JSONDecodeError:
                # Allow plain strings like "OK", "Done", etc.
                print("[OBSInterface] Non-JSON frame:", str(frame)[:120])
        except asyncio.TimeoutError:
            # idle window elapsed → stop
            break

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
    if t == "setPath":
        role = cmd.get("role")
        if role != "OBS":
            return None  # ignore paths that aren't for Shogun Live
        return f"SetPath {cmd['value']}"
    return None
