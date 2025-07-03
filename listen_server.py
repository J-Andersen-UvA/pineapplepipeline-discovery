# listen_server.py
import importlib
import asyncio
import threading

class ListenServer:
    def __init__(self, module_name, fn_name, uri_frontpoint, uri_listener):
        self.module_name    = module_name
        self.fn_name        = fn_name
        self.uri_frontpoint = uri_frontpoint
        self.uri_listener   = uri_listener
        self._thread        = None
        self._loop          = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return False    # already running

        def _runner():
            # 1) import the user’s module & grab the coroutine function
            mod = importlib.import_module(self.module_name)
            fn  = getattr(mod, self.fn_name)
            # 2) new event loop in this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            # 3) run the entry point
            coro = fn(self.uri_frontpoint, self.uri_listener)
            loop.run_until_complete(coro)
            # if the entry point never returns, loop forever
            loop.run_forever()

        self._thread = threading.Thread(target=_runner, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._loop:
            # politely shut down the asyncio loop
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=1)
