import socket
import threading
import yaml
import os
from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
import tkinter as tk
from tkinter import ttk


class PineapplePipelineListener:
    """
    Zeroconf listener for Pineapple Pipeline services (_mocap._tcp.local.).
    Emits callbacks on service add/remove so you can hook into your existing code.
    """
    def __init__(self, service_type="_mocap._tcp.local.", on_service_added=None, on_service_removed=None):
        self.zeroconf = Zeroconf()
        self.service_type = service_type
        self.on_service_added = on_service_added
        self.on_service_removed = on_service_removed
        self.browser = ServiceBrowser(self.zeroconf, self.service_type, handlers=[self._on_service_state_change])

    def _on_service_state_change(self, zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            info = zeroconf.get_service_info(service_type, name)
            if info:
                addresses = []
                for addr in info.addresses:
                    if len(addr) == 4:
                        addresses.append(socket.inet_ntoa(addr))
                    else:
                        addresses.append(socket.inet_ntop(socket.AF_INET6, addr))
                txt = {k.decode(): v.decode() for k, v in info.properties.items()}
                service = {'name': name, 'addresses': addresses, 'port': info.port, 'properties': txt}
                if self.on_service_added:
                    self.on_service_added(service)
        elif state_change is ServiceStateChange.Removed:
            if self.on_service_removed:
                self.on_service_removed(name)

    def close(self):
        self.zeroconf.close()


class PineappleDiscoveryUI(tk.Tk):
    """
    Tkinter GUI: shows expected devices from YAML (red/green status)
    and Zeroconf services separately, with background DNS checks.
    """
    def __init__(self, listener: PineapplePipelineListener, config_path="config.yaml"):
        super().__init__()
        self.title("Pineapple Pipeline Discovery")
        self.geometry("500x500")
        self.listener = listener

        # Load expected devices
        self.expected = self._load_config(config_path)
        # State: {attached_name: {'hostname', 'connected', 'ip'}}
        self.device_states = {
            dev['attached_name']: {'hostname': dev['hostname'], 'connected': False, 'ip': None}
            for dev in self.expected
        }
        self.zeroconf_services = {}

        self._build_widgets()
        self.listener.on_service_added = self._add_zeroconf_service
        self.listener.on_service_removed = self._remove_zeroconf_service

        # Start background thread for device reachability checks
        self._running = True
        self._checker_thread = threading.Thread(target=self._device_check_loop, daemon=True)
        self._checker_thread.start()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_config(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path) as f:
            data = yaml.safe_load(f)
        return data.get('devices', [])

    def _build_widgets(self):
        container = ttk.Frame(self, padding=10)
        container.pack(fill=tk.BOTH, expand=True)

        ttk.Label(container, text="Configured Devices:").pack(anchor=tk.W)
        self.expected_list = tk.Listbox(container, height=10)
        self.expected_list.pack(fill=tk.BOTH, expand=False)
        for idx, name in enumerate(self.device_states):
            self.expected_list.insert(tk.END, name)
            self.expected_list.itemconfig(idx, fg='red')

        ttk.Label(container, text="Discovered Zeroconf Services:").pack(anchor=tk.W, pady=(10,0))
        self.zc_list = tk.Listbox(container, height=10)
        self.zc_list.pack(fill=tk.BOTH, expand=True)

        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill=tk.X, pady=8)
        ttk.Button(btn_frame, text="Rescan Zeroconf", command=self._rescan).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Select Device", command=self._select_device).pack(side=tk.RIGHT)

    def _add_zeroconf_service(self, service):
        name = service['name']
        if name not in self.zeroconf_services:
            self.zeroconf_services[name] = service
            self.after(0, lambda: self.zc_list.insert(tk.END, name))

    def _remove_zeroconf_service(self, name):
        if name in self.zeroconf_services:
            idx = list(self.zeroconf_services).index(name)
            del self.zeroconf_services[name]
            self.after(0, lambda: self.zc_list.delete(idx))

    def _rescan(self):
        self.zc_list.delete(0, tk.END)
        self.zeroconf_services.clear()

    def _select_device(self):
        sel = self.expected_list.curselection() or self.zc_list.curselection()
        if not sel:
            return
        if self.expected_list.curselection():
            name = self.expected_list.get(sel[0])
            state = self.device_states[name]
            print(f"Selected configured: {name} @ {state['ip']}")
        else:
            name = self.zc_list.get(sel[0])
            svc = self.zeroconf_services[name]
            print(f"Selected zeroconf: {name} @ {svc['addresses'][0]}:{svc['port']}")

    def _device_check_loop(self):
        while self._running:
            for idx, (name, state) in enumerate(self.device_states.items()):
                try:
                    ip = socket.gethostbyname(state['hostname'])
                    if not state['connected'] or state['ip'] != ip:
                        state['connected'] = True
                        state['ip'] = ip
                        self.after(0, lambda i=idx: self.expected_list.itemconfig(i, fg='green'))
                except socket.gaierror:
                    if state['connected']:
                        state['connected'] = False
                        state['ip'] = None
                        self.after(0, lambda i=idx: self.expected_list.itemconfig(i, fg='red'))
            # Sleep between rounds
            threading.Event().wait(2)

    def _on_close(self):
        self._running = False
        self.listener.close()
        self.destroy()


if __name__ == '__main__':
    listener = PineapplePipelineListener()
    app = PineappleDiscoveryUI(listener, config_path="requiredDevices.yaml")
    app.mainloop()
