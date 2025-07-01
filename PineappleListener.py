import socket
import threading
import yaml
import os
import json
import time
import asyncio
import tkinter as tk
from tkinter import ttk, messagebox
from http.server import BaseHTTPRequestHandler, HTTPServer
from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
import websockets

import tkinterStyle as tkstyle
from PluginManager import PluginManager  # your step-2 script

MAX_MESSAGE_LENGTH = 100  # max length of messages in the UI

class DiscoveryService:
    def __init__(self, config_path='config.yaml', zeroconf_type: str = '_mocap._tcp.local.'):
        # 1) Load expected devices
        self.config = self._load_config(config_path)
        self.expected = self.config[0]
        self.device_states = {
            d['attached_name']: {'hostname': d['hostname'], 'ip': None, 'connected': False, 'checked': d.get('checked', False)}
            for d in self.expected
        }
        self._device_subscribers = []
        self._command_subscribers = []
        self._health_interval = 2.0
        self.zeroconf_type = zeroconf_type

    def start(self):
        # 2) DNS-based polling
        self._running = True
        threading.Thread(target=self._dns_poll_loop, daemon=True).start()

        # 3) Zeroconf browse + TCP-probe cleanup
        self.zeroconf = Zeroconf()
        self._zc_services = {}
        self._zc_browser = ServiceBrowser(
            self.zeroconf, self.zeroconf_type, handlers=[self._on_zc_state_change]
        )
        threading.Thread(target=self._zc_cleanup_loop, daemon=True).start()

        # 4) HTTP endpoint for JSON POSTs
        self.server = self.config[1]
        http_address, http_port = self.server.get('http_addr'), self.server.get('http_port')
        self._http_server = HTTPServer((http_address, http_port), self._make_handler())
        threading.Thread(target=self._http_server.serve_forever, daemon=True).start()

        # 5) WebSocket server for JSON messages
        self._ws_port, self._ws_address = self.server.get('ws_port'), self.server.get('ws_address')
        threading.Thread(target=self._start_ws_server, daemon=True).start()

        # 6) Start health check loop
        threading.Thread(target=self._health_loop, daemon=True).start()

        # 7) internal health‐response tracking
        # last time each device replied
        self._last_health_response = { name: 0.0 for name in self.device_states }
        # subscribe to our own command bus to catch health_response
        self.subscribe_commands(self._on_internal_command)
        # start timeout monitor
        threading.Thread(target=self._health_timeout_loop, daemon=True).start()


    def _load_config(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path) as f:
            data = yaml.safe_load(f)
        return (data.get('devices', []), data.get('server', None))

    def subscribe_devices(self, cb):
        self._device_subscribers.append(cb)

    def subscribe_commands(self, cb):
        self._command_subscribers.append(cb)

    def _notify_device(self, name, ip):
        for cb in list(self._device_subscribers):
            try: cb(name, ip)
            except: pass

    def _notify_command(self, cmd):
        """ Notify all command subscribers with a command dict.
        The command dict should have a 'type' key
        and can contain any other data relevant to the command.
        """
        for cb in list(self._command_subscribers):
            try: cb(cmd)
            except:
                print(f"[DiscoveryService] Command handler failed: {cb} – {cmd}")

    def set_device_filter(self, fn):
        """fn(name:str) -> bool; only True devices get health checks."""
        for name, state in self.device_states.items():
            state['checked'] = fn(name)

    def _dns_poll_loop(self):
        while self._running:
            for name, state in self.device_states.items():
                try:
                    ip = socket.gethostbyname(state['hostname'])
                    if not state['connected'] or state['ip'] != ip:
                        state['connected'], state['ip'] = True, ip
                        print(f"[DiscoveryService] Device {name} connected at {ip}")
                        self._notify_device(name, ip)
                        # Notify the UI log
                        self._notify_command({
                            'type': 'dns',
                            'name': name,
                            'ip': ip
                        })
                except socket.gaierror:
                    if state['connected']:
                        state['connected'], state['ip'] = False, None
                        # self._notify_device(name, None)
                        print(f"[DiscoveryService] Device {name} disconnected")
            time.sleep(2)

    def _on_zc_state_change(self, zeroconf, service_type, name, state_change):
        print(f"[DiscoveryService] Zeroconf event: {state_change.name} – {name}")
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            info = zeroconf.get_service_info(service_type, name)
            if not info: return
            addrs = [
                socket.inet_ntoa(r) if len(r)==4
                else socket.inet_ntop(socket.AF_INET6, r)
                for r in info.addresses
            ]
            cmd = {
                'type': 'zeroconf',
                'name': name,
                'addresses': addrs,
                'port': info.port,
                'properties': {
                    k.decode(): v.decode() for k, v in info.properties.items()
                }
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
                    print(f"[DiscoveryService] TCP-probe removing: {name}")
                    # funnel through the Removed handler
                    self._on_zc_state_change(
                        self.zeroconf, self.zeroconf_type, name, ServiceStateChange.Removed
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
        """
        Immediately re-poll all configured hostnames via DNS
        and fire device-up/down events.
        """
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

    def _start_ws_server(self):
        # 1) Create & set your new loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # 2) Start the WebSocket server inside a coroutine so that
        #    get_running_loop() will succeed.
        async def _run_server():
            server = await websockets.serve(
                self._ws_handler,
                self._ws_address,
                self._ws_port,
                family=socket.AF_INET   # IPv4-only to avoid any OS hiccups
            )
            return server

        # This will actually spin up the server in our new loop
        self._ws_server = loop.run_until_complete(_run_server())
        print(f"[PineappleListener] WebSocket server listening on {self._ws_port}")

        # Save the loop for later use
        self._ws_loop = loop

        # 3) Now run the loop forever in this thread
        try:
            loop.run_forever()
        finally:
            # graceful shutdown
            self._ws_server.close()
            loop.run_until_complete(self._ws_server.wait_closed())
            loop.close()

    async def _ws_handler(self, websocket, path=''):
        async for raw in websocket:
            try:
                cmd = json.loads(raw)
                self._notify_command(cmd)
            except:
                pass

    def _health_loop(self):
        """
        Every self._health_interval seconds, send a
        {'type':'health','device':<attached_name>}
        command for each currently connected device.
        Plugins will receive this and must reply with
        a 'health_response' message to clear their status.
        """
        while self._running:
            for name, state in self.device_states.items():
                if state.get('connected') and state.get('checked', True):
                    # emit a health‐check command
                    self._notify_command({
                        'type':   'health',
                        'device': name
                    })
            time.sleep(self._health_interval)

    def _on_internal_command(self, cmd):
        # catch only health_response messages
        if cmd.get('type') == 'health_response':
            dev = cmd.get('device')
            # stamp the time they replied
            if dev in self._last_health_response:
                self._last_health_response[dev] = time.time()

    def _health_timeout_loop(self):
        """
        Run in its own thread: any device that hasn't replied
        within health_interval seconds gets a one-off health_timeout.
        """
        while self._running:
            now = time.time()
            for name, state in self.device_states.items():
                if state['connected'] and state.get('checked', True):
                    last = self._last_health_response.get(name, 0.0)
                    if now - last > self._health_interval + 0.5:  # allow a small grace period
                        # they've timed out!
                        # reset so we only fire once until they reply again
                        self._last_health_response[name] = now
                        self._notify_command({
                            'type':   'health_timeout',
                            'value': name
                        })
            # check twice as often as health requests
            time.sleep(self._health_interval * 0.5)

    def shutdown(self):
        self._running = False
        self.zeroconf.close()
        self._http_server.shutdown()
        self._http_server.server_close()


class StyledDiscoveryUI(tkstyle.DiscoveryUI):
    def __init__(self, master, service: DiscoveryService):
        super().__init__(master)
        self.service = service
        master.protocol("WM_DELETE_WINDOW", self._on_close)

        # Clear placeholders in styled frames
        for frame in (self.configured_devices, self.zeroconf):
            for child in frame.winfo_children():
                child.destroy()

        # Configured devices as checkboxes
        self.device_vars = {}
        self.device_buttons = {}
        self.device_hearts = {}
        for d in service.expected:
            name = d['attached_name']
            var = tk.BooleanVar(value=True)

            row = ttk.Frame(self.configured_devices)
            row.pack(fill=tk.X, padx=5, pady=2)

            # Checkbox for device
            cb = tk.Checkbutton(row, text=name, variable=var, anchor='w', fg='red', command=lambda n=name: self._on_check_toggle(n))

            # heart icon, default gray
            heart = ttk.Label(row, text="💚", foreground='gray')
            heart.pack(side=tk.LEFT, padx=(0,5))
            self.device_hearts[name] = heart

            cb.pack(fill=tk.X, padx=5, pady=2)
            self.device_vars[name] = var
            self.device_buttons[name] = cb

        # Zeroconf services checkboxes
        self.zc_vars = {}
        self.zc_buttons = {}

        # Last Messages list
        self.msg_list = tk.Listbox(self.last_messages, height=6)
        self.msg_list.pack(fill=tk.X, padx=5, pady=5)

        # Current status area
        btn_frame = ttk.Frame(self.current_status)
        btn_frame.pack(pady=5)
        self.status_label = ttk.Label(
            self.current_status, text="Status: Idle"
        )
        self.status_label.pack(pady=(5,0))

        # Button area
        frame = ttk.Frame(self.button_area)
        frame.pack(pady=5)
        ttk.Button(frame, text="Show IP", command=self._show_ip)\
            .pack(side=tk.LEFT, padx=(0,10))
        ttk.Button(frame, text="Rescan", command=self._on_rescan)\
            .pack(side=tk.LEFT)

        # Subscribe to device + command events
        service.subscribe_devices(self._on_device_event)
        service.subscribe_commands(self._on_command_event)

        # Force initial DNS update
        # self.service.rescan_devices()

    def _on_device_event(self, name, ip):
        cb = self.device_buttons.get(name)
        # if not cb:
        #     return

        # Decide color and text
        if ip:
            display = f"{name} ({ip})"
            color   = 'green'
        else:
            display = name
            color   = 'red'

        # Since we're in a callback thread, marshal back to the UI thread
        def _update():
            cb.config(text=display, fg=color)

        self.after(0, _update)

    def _on_command_event(self, cmd):
        ctype = cmd.get('type')
        # prefer 'value' if it exists, otherwise fall back to 'name'
        disp = cmd.get('value') or cmd.get('name') or '<unknown>'
        check_health = ctype == "health_response" and not cmd.get('value')
        name  = cmd.get('device') or cmd.get('name')

        # Log in Last Messages
        if check_health or ctype not in ("health_response"):
            disp = f"{disp} (unhealthy)"
            self.after(0, lambda: self.msg_list.insert(
                tk.END,
                f"{time.strftime('%H:%M:%S')} – {ctype}: {disp}"
            ))
        # Update status label
        self.after(0, lambda: self.status_label.config(
            text=f"Last: {ctype} – {disp}"
        ))
        # Scroll to bottom
        self.after(0, lambda: self.msg_list.see(tk.END))

        # Trim the top of the list if too long
        if self.msg_list.size() > MAX_MESSAGE_LENGTH:
            self.after(0, lambda: self.msg_list.delete(0))

        # Manage Zeroconf checkboxes
        if ctype == 'zeroconf' and disp not in self.zc_vars:
            var = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(
                self.zeroconf, text=disp,
                variable=var, anchor='w'
            )
            cb.pack(fill=tk.X, padx=5, pady=2)
            self.zc_vars[disp] = var
            self.zc_buttons[disp] = cb

        elif ctype == 'zeroconf_removed':
            cb = self.zc_buttons.pop(disp, None)
            if cb:
                self.after(0, cb.destroy)
            self.zc_vars.pop(disp, None)
        
        elif ctype == 'health_response':
            dev = cmd['device']
            ok  = cmd.get('value', False)
            heart = self.device_hearts.get(dev)
            if heart:
                color = 'green' if ok else 'red'
                self.after(0, lambda c=color, h=heart: h.config(foreground=c))

        elif ctype == "health":
            # only animate if that device is checked
            if self.device_vars.get(name, False).get():
                self._beat_heart(name)
            # we don’t need to log health‐checks themselves, so return
            return

    def _on_check_toggle(self, toggled_name):
        """
        Re-install the device_filter on the DiscoveryService so that
        health checks only go to checked devices.
        """
        # build a predicate that returns True only for currently checked names
        predicate = lambda dev_name: self.device_vars[dev_name].get()
        self.service.set_device_filter(predicate)

        # reset the heart icon to gray if they just turned it off
        if toggled_name is not None and not self.device_vars[toggled_name].get():
            heart = self.device_hearts.get(toggled_name)
            if heart:
                # 🖤 or gray 💚 for “inactive”
                heart.config(text='💚', foreground='gray')

    def _beat_heart(self, name):
        """
        Swap the heart to 💓, then back to 💚 after 200ms.
        """
        heart = self.device_hearts.get(name)
        # if no heart or device is unchecked, do nothing
        if not heart or not self.device_vars.get(name, False).get():
            return

        # show beating heart
        heart.config(text='💓')
        # then after a short delay, restore the normal heart
        self.after(200, lambda: heart.config(text='💚'))


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
        # clear Zeroconf checkboxes
        for cb in self.zc_buttons.values():
            cb.destroy()
        self.zc_vars.clear()
        self.zc_buttons.clear()
        # clear messages & status
        self.msg_list.delete(0, tk.END)
        self.status_label.config(text="Status: Rescanning…")
        # do rescans
        self.service.rescan_zeroconf()
        self.service.rescan_devices()

    def _on_close(self):
        self.service.shutdown()
        self.master.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    tkstyle.init_style(root)
    root.minsize(600, 600)     # minimum width=600px, height=400px
    root.title("Pineapple Listener UI")

    print("Initializing Discovery Service...")
    disco = DiscoveryService('config.yaml')

    print("Initializing UI...")
    ui = StyledDiscoveryUI(root, disco)
    ui.pack(fill=tk.BOTH, expand=True)
    ui._on_check_toggle(None)
    print("UI initialized.")
    disco.start()  # start the discovery service
    print("Discovery Service started.")

    # Load plugins (scripts) and prepare dispatch
    plugin_mgr = PluginManager(disco.expected, disco._notify_command)

    def _dispatch_to_plugins(cmd):
        # ignore Zeroconf internal events
        if cmd.get('type') in ('zeroconf', 'zeroconf_removed'):
            return
        # send to checked plugins
        for name, var in ui.device_vars.items():
            if not var.get(): continue

            # grab the last-known IP for this device…
            ip = disco.device_states[name]['ip']

            # …and merge it into the command dict
            enriched = dict(cmd, ip=ip)

            plugin_mgr.handle(name, enriched)

    disco.subscribe_commands(_dispatch_to_plugins)

    root.mainloop()
