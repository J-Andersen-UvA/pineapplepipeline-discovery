import asyncio
import websockets
import json
import yaml

config = yaml.safe_load(open('config.yaml', 'r'))

async def send_example():
    uri = f"ws://{config[1].get('ws_port')}:{config[1].get('ws_address')}/"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type":"example","message":"example msg"}))

asyncio.run(send_example())
