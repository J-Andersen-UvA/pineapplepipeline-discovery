import socket
import threading
import yaml
import os
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange

import tkinter as tk
from tkinter import ttk, messagebox

import tkinterStyle as tkstyle  # your styling module


class DiscoveryService:
    def __init__(
        self,
        config_path='config.yaml',
        http_port=8000,
        zeroconf_type: str = '_mocap._tcp.local.'
    ):
        # load expected devices
        self.expected = self._load_config(config_path)
        self.device_states = {
            d['attached_name']: {
                'hostname': d['hostname'],
                'ip': None,
                'connected': False
            }
            for d in self.expected
        }
        self._device_subscribers = []
        self._command_subscribers = []

        # DNS poller
        self._running = True
        threading.Thread(target=self._dns_poll_loop, daemon=True).start()

        # Zeroconf browser + cache
        self.zeroconf = Zeroconf()
        self.zeroconf_type = zeroconf_type
        self._zc_services = {}
        self._zc_browser = ServiceBrowser(
            self.zeroconf,
            zeroconf_type,
            handlers=[self._on_zc_state_change]
        )

        # TCP-probe cleanup
        threading.Thread(target=self._zc_cleanup_loop, daemon=True).start()

        # HTTP endpoint for external commands
        self._http_server = HTTPServer(
            ('0.0.0.0', http_port),
            self._make_handler()
        )
        threading.Thread(
            target=self._http_server.serve_forever,
            daemon=True
        ).start()


    def _load_config(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path) as f:
            data = yaml.safe_load(f)
        return data.get('devices', [])


    def subscribe_devices(self, cb):
        self._device_subscribers.append(cb)


    def subscribe_commands(self, cb):
        self._command_subscribers.append(cb)


    def _notify_device(self, name, ip):
        for cb in list(self._device_subscribers):
            try: cb(name, ip)
            except: pass


    def _notify_command(self, cmd):
        for cb in list(self._command_subscribers):
            try: cb(cmd)
            except:
                print(f"[DiscoveryService] Command handler failed: {cb} – {cmd}")


    def _dns_poll_loop(self):
        while self._running:
            for name, state in self.device_states.items():
                try:
                    ip = socket.gethostbyname(state['hostname'])
                    if not state['connected'] or state['ip'] != ip:
                        state['connected'], state['ip'] = True, ip
                        self._notify_device(name, ip)
                except socket.gaierror:
                    if state['connected']:
                        state['connected'], state['ip'] = False, None
                        self._notify_device(name, None)
            time.sleep(2)


    def _on_zc_state_change(self, zeroconf, service_type, name, state_change):
        print(f"[DiscoveryService] Zeroconf event: {state_change.name} – {name}")
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            info = zeroconf.get_service_info(service_type, name)
            if not info:
                return
            addrs = []
            for raw in info.addresses:
                if len(raw) == 4:
                    addrs.append(socket.inet_ntoa(raw))
                else:
                    addrs.append(socket.inet_ntop(socket.AF_INET6, raw))
            cmd = {
                'type': 'zeroconf',
                'name': name,
                'addresses': addrs,
                'port': info.port,
                'properties': {k.decode(): v.decode() for k, v in info.properties.items()}
            }
            self._zc_services[name] = cmd
            self._notify_command(cmd)

        elif state_change is ServiceStateChange.Removed:
            print(f"[DiscoveryService] Zeroconf explicit removal: {name}")
            self._handle_zc_removal(name)


    def _handle_zc_removal(self, name):
        if name in self._zc_services:
            del self._zc_services[name]
            self._notify_command({'type': 'zeroconf_removed', 'name': name})


    def _zc_cleanup_loop(self):
        while self._running:
            for name, cmd in list(self._zc_services.items()):
                alive = False
                for ip in cmd['addresses']:
                    try:
                        with socket.create_connection((ip, cmd['port']), timeout=1):
                            alive = True
                            break
                    except:
                        pass
                if not alive:
                    print(f"[DiscoveryService] TCP-probe failed; firing Removed for {name}")
                    # funnel through the Removed branch
                    self._on_zc_state_change(
                        self.zeroconf,
                        self.zeroconf_type,
                        name,
                        ServiceStateChange.Removed
                    )
            time.sleep(2)


    def rescan_zeroconf(self):
        """Restart the Zeroconf browser to trigger fresh Add events."""
        try: self._zc_browser.cancel()
        except: pass
        try: self.zeroconf.close()
        except: pass
        self._zc_services.clear()
        self.zeroconf = Zeroconf()
        self._zc_browser = ServiceBrowser(
            self.zeroconf,
            self.zeroconf_type,
            handlers=[self._on_zc_state_change]
        )


    def rescan_devices(self):
        """Immediately re-poll all configured hostnames via DNS."""
        for name, state in self.device_states.items():
            try:
                ip = socket.gethostbyname(state['hostname'])
                state['connected'], state['ip'] = True, ip
            except socket.gaierror:
                state['connected'], state['ip'] = False, None
            self._notify_device(name, state['ip'])


    def _make_handler(self):
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length)
                try:
                    cmd = json.loads(raw)
                    parent._notify_command(cmd)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'OK')
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
        return Handler


    def shutdown(self):
        self._running = False
        self.zeroconf.close()
        self._http_server.shutdown()
        self._http_server.server_close()



class StyledDiscoveryUI(tkstyle.DiscoveryUI):
    def __init__(self, master, service: DiscoveryService):
        """
        master: the root tk.Tk()
        service: our DiscoveryService instance
        """
        super().__init__(master)
        self.service = service

        # configure close protocol on the root window, not the frame
        master.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- Configured Devices Section ----
        self.device_vars = {}
        self.device_buttons = {}
        for d in service.expected:
            name = d['attached_name']
            var = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(
                self.configured_devices,  # from your style file
                text=name,
                variable=var,
                anchor='w',
                fg='red'
            )
            cb.pack(fill=tk.X, padx=5, pady=2)
            self.device_vars[name] = var
            self.device_buttons[name] = cb

        # ---- Zeroconf Services Section ----
        self.zc_vars = {}
        self.zc_buttons = {}

        # ---- Last Messages Section ----
        self.msg_list = tk.Listbox(self.last_messages, height=6)
        self.msg_list.pack(fill=tk.X, padx=5, pady=5)

        # ---- Current Status Section ----
        btn_frame = ttk.Frame(self.current_status)
        btn_frame.pack(pady=5)
        ttk.Button(btn_frame, text="Show IP addresses", command=self._show_ip)\
            .pack(side=tk.LEFT, padx=(0,10))
        ttk.Button(btn_frame, text="Rescan", command=self._on_rescan)\
            .pack(side=tk.LEFT)
        self.status_label = ttk.Label(
            self.current_status,
            text="Status: Idle"
        )
        self.status_label.pack(pady=(5,0))

        # subscribe to service events
        service.subscribe_devices(self._on_device_event)
        service.subscribe_commands(self._on_command_event)


    def _on_device_event(self, name, ip):
        cb = self.device_buttons.get(name)
        if cb:
            color = 'green' if ip else 'red'
            self.after(0, lambda c=color, b=cb: b.config(fg=c))


    def _on_command_event(self, cmd):
        ctype = cmd.get('type')
        name = cmd.get('name')

        # log in Last Messages
        self.after(0, lambda: self.msg_list.insert(
            tk.END,
            f"{time.strftime('%H:%M:%S')} – {ctype}: {name}"
        ))
        # update status label
        self.after(0, lambda: self.status_label.config(
            text=f"Last: {ctype} – {name}"
        ))

        # Manage Zeroconf checkbuttons
        if ctype == 'zeroconf' and name not in self.zc_vars:
            var = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(
                self.zeroconf,  # from style file
                text=name,
                variable=var,
                anchor='w'
            )
            cb.pack(fill=tk.X, padx=5, pady=2)
            self.zc_vars[name] = var
            self.zc_buttons[name] = cb

        elif ctype == 'zeroconf_removed':
            cb = self.zc_buttons.pop(name, None)
            if cb:
                self.after(0, cb.destroy)
            self.zc_vars.pop(name, None)


    def _show_ip(self):
        checked = [n for n, var in self.device_vars.items() if var.get()]
        if not checked:
            messagebox.showinfo("Show IP", "No device selected.")
            return
        info = "\n".join(
            f"{n}: {self.service.device_states[n].get('ip') or '<not connected>'}"
            for n in checked
        )
        messagebox.showinfo("Device IPs", info)


    def _on_rescan(self):
        # clear Zeroconf section
        for cb in self.zc_buttons.values():
            cb.destroy()
        self.zc_vars.clear()
        self.zc_buttons.clear()
        # clear messages & update status
        self.msg_list.delete(0, tk.END)
        self.status_label.config(text="Status: Rescanning…")
        # trigger rescans
        self.service.rescan_zeroconf()
        self.service.rescan_devices()


    def _on_close(self):
        self.service.shutdown()
        self.master.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    tkstyle.init_style(root)   # apply your theme/fonts

    disco = DiscoveryService('config.yaml', http_port=8000)
    ui = StyledDiscoveryUI(root, disco)
    ui.pack(fill=tk.BOTH, expand=True)

    root.mainloop()
