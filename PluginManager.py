import importlib.machinery, importlib.util

class PluginManager:
    def __init__(self, devices, send_response):
        self._plugins = {}
        for dev in devices:
            name = dev['attached_name']
            path = dev['script']
            # load module from path
            loader = importlib.machinery.SourceFileLoader(name, path)
            spec   = importlib.util.spec_from_loader(loader.name, loader)
            mod    = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            # initialize it (gives it the callback + its own config)
            mod.init(lambda msg, n=name: send_response({'device': n, **msg}), dev)
            self._plugins[name] = mod

    def handle(self, device_name, msg):
        # find module & call its handler
        if device_name in self._plugins:
            try:
                self._plugins[device_name].handle_message(msg)
            except Exception as e:
                # bubble error back to UI
                self._send_response({'error': str(e)})
