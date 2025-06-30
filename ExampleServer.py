import asyncio
import websockets
import json

async def send_example():
    uri = "ws://localhost:8765/"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type":"example","message":"example msg"}))

asyncio.run(send_example())
