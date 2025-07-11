import importlib.machinery, importlib.util
import os

class PluginManager:
    def __init__(self, devices, send_response):
        self._plugins = {}
        self._configs = {}
        for dev in devices:
            name = dev['attached_name']
            raw_path = dev['script']

            # Make path absolute and normalized
            path = os.path.expanduser(raw_path)
            path = os.path.expandvars(path)
            if not os.path.isabs(path):
                # Resolve relative to this file’s directory:
                base_dir = os.path.dirname(__file__)
                path = os.path.normpath(os.path.join(base_dir, path))
            else:
                path = os.path.normpath(path)

            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"Plugin script '{path}' does not exist for device '{name}'."
                )

            # load module from path
            loader = importlib.machinery.SourceFileLoader(name, path)
            spec   = importlib.util.spec_from_loader(loader.name, loader)
            mod    = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            # initialize it (gives it the callback + its own config)
            mod.init(lambda msg, n=name: send_response({'device': n, **msg}), dev)
            self._plugins[name] = mod
            self._configs[name] = dev

    def handle(self, device_name, msg):
        # find module & call its handler
        if device_name in self._plugins:
            try:
                sub_entry = self._configs[device_name].get('subname', None)
                if sub_entry is not None and sub_entry != '':
                    # if the plugin has a subname, add it to the message
                    msg['subname'] = self._configs[device_name]['subname']
                self._plugins[device_name].handle_message(msg)
            except Exception as e:
                print(f"[PluginManager] Error in plugin {device_name}: {e}")
                # bubble error back to UI
                self._send_response({'error': str(e)})
