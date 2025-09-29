import sys
import asyncio
import websockets
import ssl
import json
import yaml
from urllib.parse import urlparse

# WS_ADDR = "localhost"  # Default address for WebSocket server
# WS_PORT = 5000  # Default port for WebSocket server

async def send_message(msg: dict, uri):
    print(f"[UEServer] → sending to Listener {uri}: {msg}")
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps(msg))

async def handle_message(msg: dict, uri_listener: str):
    """
    Accepts multiple schema variants from the website(s) and maps them to
    Pineapple events the Listener already understands:
      - recordStart / recordStop
      - fileName (broadcastGlos / fileName)
    """
    # pull common fields
    set_value     = msg.get('set', '')         # e.g. 'fileName', 'startRecord', 'stopRecord'
    handler_value = msg.get('handler', '')     # e.g. 'startCapture', 'stopCapture', 'glosName'
    data_value    = msg.get('data', '')        # sometimes duplicates handler verb or 'broadcastGlos'
    value         = msg.get('value')           # used by some variants for the filename
    glos          = msg.get('glos')            # used by the production site

    print(f"[UEServer] Received message: {msg}")
    print(f"[UEServer] Parsed set={set_value!r} handler={handler_value!r} data={data_value!r}")

    # --- start / stop capture (support both shapes) ---
    if handler_value == "startCapture" or data_value == "startCapture" or set_value == "startRecord":
        print("[UEServer] → recordStart")
        await send_message({"type": "recordStart", "value": "starting the recording"}, uri_listener)
        return

    if handler_value == "stopCapture" or data_value == "stopCapture" or set_value == "stopRecord":
        print("[UEServer] → recordStop")
        await send_message({"type": "recordStop", "value": "stopping the recording"}, uri_listener)
        return

    # --- broadcast / set filename (support both shapes) ---
    # 1) production site: { data:"broadcastGlos", glos:"..." }
    if data_value == "broadcastGlos" and glos:
        print(f"[UEServer] → fileName (from broadcastGlos/glos): {glos}")
        await send_message({"type": "fileName", "value": glos}, uri_listener)
        return

    # 2) older shape: { set:"broadcastGlos", value:"..." }
    if set_value == "broadcastGlos" and value:
        print(f"[UEServer] → fileName (from set:broadcastGlos/value): {value}")
        await send_message({"type": "fileName", "value": value}, uri_listener)
        return

    # 3) direct fileName set: { set:"fileName", value:"..." }
    if set_value == "fileName" and value:
        print(f"[UEServer] → fileName (from set:fileName/value): {value}")
        await send_message({"type": "fileName", "value": value}, uri_listener)
        return

    # 4) legacy: { handler:"glosName", ... } – if a filename lives in any field
    if handler_value == "glosName":
        name = value or glos
        if name:
            print(f"[UEServer] → fileName (from handler:glosName): {name}")
            await send_message({"type": "fileName", "value": name}, uri_listener)
            return

    # 5) new shape: { handler:"<filename>", set:"broadcastGlos" }
    if set_value == "broadcastGlos" and handler_value:
        print(f"[UEServer] → fileName (from handler:set:broadcastGlos): {handler_value}")
        await send_message({"type": "fileName", "value": handler_value}, uri_listener)
        return
    
    if set_value == "ping":
        return

    print("[UEServer] Invalid / unrecognized message shape")

RETRY_BACKOFF = [1, 2, 5, 10]

async def receive_messages(uri_frontpoint, uri_listener):
    parsed = urlparse(uri_frontpoint)
    ssl_context = None
    if parsed.scheme == "wss":
        # For production, VERIFY the cert; for local dev you might disable checks.
        ssl_context = ssl.create_default_context()
        # DEV ONLY – comment these out in prod:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    for delay in [0] + RETRY_BACKOFF:
        if delay:
            print(f"[UEServer] Retry connecting in {delay}s...")
            await asyncio.sleep(delay)
        try:
            print(f"[UEServer] Connecting to {uri_frontpoint} ...")
            async with websockets.connect(
                uri_frontpoint,
                ssl=ssl_context,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_queue=None
            ) as websocket:
                print("[UEServer] Connected to server")
                while True:
                    raw = await websocket.recv()
                    print("[UEServer] Received message:", raw)
                    msg = json.loads(raw)
                    await handle_message(msg, uri_listener)
        except websockets.exceptions.InvalidStatus as e:
            # Typically 4xx/5xx from proxy/origin
            status = getattr(e, "response", None)
            code = getattr(status, "status_code", None) if status else None
            print(f"[UEServer] WebSocket HTTP error: {code or 'unknown'} – "
                  f"Is the path/scheme correct and the proxy forwarding upgrades?")
        except (ConnectionRefusedError, OSError) as e:
            print(f"[UEServer] TCP connect failed: {e}")
        except websockets.ConnectionClosed as e:
            print(f"[UEServer] Connection closed: code={e.code} reason={e.reason}")
        except Exception as e:
            print(f"[UEServer] Unexpected error: {type(e).__name__}: {e}")
        # Loop to retry
    print("[UEServer] Giving up after retries.")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(f"[UEServer] Arguments received: {sys.argv}")
    else:
        print("[UEServer] No argument provided.")
        print("[UEServer] Usage: python unrealServer.py [config_path]")

    config_path = sys.argv[2] if len(sys.argv) > 2 else 'config.yaml'
    config = yaml.safe_load(open(config_path, 'r'))

    server_cfg = config.get('server', {})
    listener_host = server_cfg.get('ws_addr', 'localhost')
    listener_port = int(server_cfg.get('ws_port', 8766))
    uri_listener = f"ws://{listener_host}:{listener_port}"

    uri_frontpoint = config.get('listen_server', {}).get('uri', None) or "ws://localhost:8043/unrealServer"

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(receive_messages(uri_frontpoint, uri_listener))
        loop.run_forever()
    finally:
        loop.close()
