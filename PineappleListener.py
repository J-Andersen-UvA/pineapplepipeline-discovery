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
from listen_server import ListenServer  # your step-1 script
import websockets

import tkinterStyle as tkstyle
from PluginManager import PluginManager  # your step-2 script

MAX_MESSAGE_LENGTH = 100  # max length of messages in the UI

class DiscoveryService:
    def __init__(self, config_path='config.yaml', zeroconf_type: str = '_mocap._tcp.local.'):
        # 1) Load expected devices
        devices, server, listen_conf = self._load_config(config_path)
        self.expected = devices
        self.server = server
        self.listen_conf = listen_conf

        self.device_states = {
            d['attached_name']: {'hostname': d['hostname'], 'ip': None, 'resolved': False, 'reachable': False, 'checked': d.get('checked', False), 'subname': d.get('subname', ''), 'port': None, 'attached_subname': d.get('attached_subname', '')}
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
        return (data.get('devices', []), data.get('server', None), data.get('listen_server', {}))

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
            except Exception as e:
                print(f"[DiscoveryService] Command handler failed: {cb} – {cmd}")

    def set_device_filter(self, fn):
        """fn(name:str) -> bool; only True devices get health checks."""
        for name, state in self.device_states.items():
            state['checked'] = fn(name)

    def _dns_poll_loop(self):
        while self._running:
            for name, state in self.device_states.items():
                try:
                    ip = socket.gethostbyname(state.get('hostname', None))
                    # first time resolution or IP changed?
                    if not state['resolved'] or state['ip'] != ip:
                        state['resolved'], state['ip'] = True, ip
                        # reset reachability when it reappears
                        state['reachable'] = False
                        print(f"[DiscoveryService] Device {name} connected at {ip}")
                        self._notify_device(name, ip)
                        # Notify the UI log
                        self._notify_command({
                            'type': 'dns',
                            'name': name,
                            'ip': ip
                        })
                except socket.gaierror:
                    # couldn’t resolve — mark it disconnected (once)
                    if state['resolved']:
                        state['resolved'] = False
                        print(f"[DiscoveryService] Device {name} disconnected, ip cached. State: {state}")
                        self._notify_device(name, None)
                        self._notify_command({
                            'type': 'dns',
                            'name': name,
                            'ip': None
                        })


                # Also try to find sub devices in the subname
                if state.get('subname', None):
                    try:
                        sub_ip = socket.gethostbyname(state['subname'])
                        # store it on the parent’s state
                        if sub_ip != state.get('sub_ip'):
                            state['sub_ip'] = sub_ip
                            print(f"[DiscoveryService] Sub-device {state['subname']} connected at {sub_ip}")
                            state['sub_ip'] = sub_ip
                            self._notify_command({
                                'type': 'dns_sub',
                                'name':    name,
                                'subname': state['subname'],
                                'ip':      sub_ip
                            })
                    except socket.gaierror:
                        # sub device not found, ignore
                        pass
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
            print(f"[DiscoveryService] Adding Zeroconf service: {name} @ {addrs}:{info.port}")
            cmd = {
                'type': 'zeroconf',
                'name': name,
                'addresses': addrs,
                'port': info.port,
                'properties': {
                    k.decode(): v.decode() for k, v in info.properties.items()
                }
            }
            name = self._check_zc_in_devices(name)
            if name:
                self.device_states[name]['ip']   = addrs[0]
                self.device_states[name]['port'] = info.port
                self.device_states[name]['resolved'] = True

            self._zc_services[name] = cmd
            self._zc_service_to_device(cmd)
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

    def _check_zc_in_devices(self, name):
        """
        Check if a Zeroconf service name matches any of the expected devices.
        Returns the device name if found, otherwise None.
        """
        for device in self.expected:
            if device['attached_name'] == name or name.startswith(device['attached_name']):
                return device['attached_name']
            elif device['hostname'] == name or name.startswith(device['hostname']):
                return device['attached_name']
        return None
    
    def _zc_service_to_device(self, cmd):
        """
        Convert a Zeroconf service command to a device state.
        Returns the device name if it matches an expected device, otherwise None.
        """
        name = cmd.get('name')
        if not name:
            print("[DiscoveryService] Zeroconf command has no name, skipping")
            return None
        dev_name = self._check_zc_in_devices(name)
        if dev_name:
            # update the device state with Zeroconf info
            state = self.device_states[dev_name]
            state['ip'] = cmd.get('addresses', [None])[0]
            state['port'] = cmd.get('port')
            print(f"[DiscoveryService] Device '{dev_name}' updated with Zeroconf info: {state}")
            state['resolved'] = True
            state['checked'] = True
            ip_port = f"{state['ip']}:{state['port']}"
            self._notify_device(dev_name, ip_port)
        else:
            print(f"[DiscoveryService] Zeroconf service '{name}' does not match any expected device")

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
                if state.get('ip') and state.get('checked', True):
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
            state = self.device_states.get(dev)
            if state:
                state['reachable'] = bool(cmd.get('value', False))
                self._last_health_response[dev] = time.time()

    def _health_timeout_loop(self):
        """
        Run in its own thread: any device that hasn't replied
        within health_interval seconds gets a one-off health_timeout.
        """
        while self._running:
            now = time.time()
            for name, state in self.device_states.items():
                if state['resolved'] and state.get('checked', True):
                    last = self._last_health_response.get(name, 0.0)
                    if now - last > self._health_interval + 0.5:  # allow a small grace period
                        # they've timed out!
                        state['reachable'] = False
                        # reset so we only fire once until they reply again
                        self._last_health_response[name] = now
                        self._notify_command({
                            'type':   'health_timeout',
                            'value': name
                        })
                    else:
                        state['reachable'] = True
            # check twice as often as health requests
            time.sleep(self._health_interval * 0.5)

    def restart(self):
        """
        Fully restart DNS polling, Zeroconf browsing,
        HTTP & WebSocket servers—and clear all old UI state.
        """
        print("[DiscoveryService] Restarting…")

        # 1) Tell the UI every Zeroconf service is gone
        for svc_name in list(self._zc_services.keys()):
            self._notify_command({'type': 'zeroconf_removed', 'name': svc_name})
        self._zc_services.clear()

        # 2) Tell the UI every configured device is now down
        for name, state in self.device_states.items():
            state['resolved'] = False
            state['ip']        = None
            self._notify_device(name, None)

        # 3) Reset health‐response timers so we’ll re‐timeout properly
        self._last_health_response = {name: 0.0 for name in self.device_states}

        # 4) Tear everything down
        self.shutdown()
        time.sleep(0.2)  # give sockets & threads a moment to unwind

        # 5) Bring it all back up
        self.start()
        print("[DiscoveryService] Restart complete.")

    def shutdown(self):
        self._running = False

        # Zeroconf
        try:
            self.zeroconf.close()
        except:
            pass

        # HTTP server
        try:
            self._http_server.shutdown()
            self._http_server.server_close()
        except:
            pass

        # WebSocket server
        try:
            # close the server sockets
            self._ws_server.close()
        except:
            pass

        try:
            # tell the asyncio loop to stop
            self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
        except:
            pass


class StyledDiscoveryUI(tkstyle.DiscoveryUI):
    def __init__(self, master, service: DiscoveryService):
        super().__init__(master)
        self.service = service
        master.protocol("WM_DELETE_WINDOW", self._on_close)
        self.healthy = ""
        self.status = "Idle"

        # === instantiate the listener ===
        lc = self.service.listen_conf or {}
        module      = lc.get("module")
        entrypt     = lc.get("entrypoint", "receive_messages")
        uri         = lc.get("uri", {})
        uri_listener = f"ws://{self.service.server.get('ws_address', 'localhost')}:{self.service.server.get('ws_port', 8766)}"
        self._listen = ListenServer(module, entrypt, uri, uri_listener)

        # Clear placeholders in styled frames
        for frame in (self.configured_devices, self.zeroconf):
            for child in frame.winfo_children():
                child.destroy()

        # Configured devices as checkboxes
        self.device_vars = {}
        self.device_buttons = {}
        self.device_hearts = {}
        self.device_sub_labels = {}
        for d in service.expected:
            name = d['attached_name']
            var = tk.BooleanVar(value=True)

            row = ttk.Frame(self.configured_devices)
            row.pack(fill=tk.X, padx=5, pady=2)

            # Checkbox for device
            cb = tk.Checkbutton(row, text=name, variable=var, anchor='w', fg='gray80', command=lambda n=name: self._on_check_toggle(n))

            # If the device has a subname, add new entry just text under the main device
            if d.get('attached_subname', '') != '':
                subname = d['attached_subname']
                sub_label = ttk.Label(row, text=f"  (sub-device: {subname})", foreground='gray60')
                sub_label.pack(side=tk.BOTTOM, padx=(10,0))
                self.device_sub_labels[name] = sub_label

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

        # Current status area, a status text label and a current name label
        btn_frame = ttk.Frame(self.current_status)
        btn_frame.pack(pady=5)
        self.status_label = ttk.Label(
            self.current_status, text=f"Status:\t{self.status} {self.healthy}",
        )
        self.status_label.pack(pady=(5,0))
        self.current_name_label = ttk.Label(
            self.current_status, text="Name:\tNone"
        )
        self.current_name_label.pack(pady=(0,5))

        # Button area
        frame = ttk.Frame(self.button_area)
        frame.pack(pady=5)
        self.listen_button = ttk.Button(frame, text="Start Listen", command=self._on_listen_toggle)
        self.listen_button.pack(side=tk.LEFT, padx=(0,10))
        ttk.Button(frame, text="Restart", command=self._on_restart)\
            .pack(side=tk.LEFT)

        # Subscribe to device + command events
        service.subscribe_devices(self._on_device_event)
        service.subscribe_commands(self._on_command_event)

        # Force initial DNS update
        # self.service.rescan_devices()

    def _on_device_event(self, name, ip_and_port):
        cb = self.device_buttons.get(name)
        state = self.service.device_states[name]
        if ip_and_port:
            state['ip'] = ip_and_port.split(':')[0]
            state['port'] = ip_and_port.split(':')[1] if ':' in ip_and_port else "None"

        # Decide color
        if state['resolved']:
            display = f"{name} ({state['ip']}:{state['port']})"
            color   = 'black'
        elif state['ip']:
            display = f"{name} (cached: {state['ip']}:{state['port']})"
            color   = 'gray60'
        else:
            display = f"{name}"
            color   = 'gray80'

        # Since we're in a callback thread, marshal back to the UI thread
        def _update():
            cb.config(text=display, fg=color)

            # whenever we lose IP, reset the heart to neutral gray
            heart = self.device_hearts.get(name)
            if heart and not state['ip']:
                heart.config(text='💚', foreground='gray')

        self.after(0, _update)

    def _on_command_event(self, cmd):
        ctype = cmd.get('type')
        # prefer 'value' if it exists, otherwise fall back to 'name'
        disp = cmd.get('value') or f"{cmd.get('name')}:{cmd.get('port')}" or '<unknown>'
        check_health = ctype == "health_response" and not cmd.get('value') or ctype == "health_timeout"
        name  = cmd.get('device') or f"{cmd.get('name')}:{cmd.get('port')}" or '<unknown>'

        # Log in Last Messages
        if check_health:
            disp = f"{disp} (unhealthy)"
            self.after(0, lambda: self.msg_list.insert(
                tk.END,
                f"{time.strftime('%H:%M:%S')} – {ctype}: {cmd.get('msg', 'No message') or disp}"
            ))
            self.healthy = "unhealthy"
        elif ctype == "health_response" and cmd.get('value'):
            self.healthy = ""
        
        # Update status area, based on type
        if ctype == 'recordStart':
            self.status = "Recording"
        elif ctype == 'recordStop':
            self.status = "Idle"
        elif ctype == 'fileName':
            self.current_name_label.config(text=f"Name:\t{disp}")
            # also log the file name
            self.msg_list.insert(tk.END, f"{time.strftime('%H:%M:%S')} – File: {disp}")
        elif ctype in ('dns_sub'):
            name    = cmd['name']
            subname = cmd.get('subname','')
            ip      = cmd.get('ip')

            # 1) update the list-box log:
            ts = time.strftime('%H:%M:%S')
            if subname and ip:
                self.msg_list.insert(
                    tk.END,
                    f"{ts} – Sub-device {subname} @ {ip}"
                )
            # 2) update the little sub-label under the main checkbox:
            lbl = self.device_sub_labels.get(name)
            if lbl and subname and ip:
                # marshal back into the UI thread
                self.after(0, lambda l=lbl, s=subname, i=ip: l.config(text=f"  (sub-device: {s} @ {i})"))

        # Update the status label
        self.status_label.config(text=f"Status:\t{self.status} {self.healthy}")
        # Scroll to bottom
        self.after(0, lambda: self.msg_list.see(tk.END))

        # Trim the top of the list if too long
        if self.msg_list.size() > MAX_MESSAGE_LENGTH:
            self.after(0, lambda: self.msg_list.delete(0))        
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

    def _on_listen_toggle(self):
        ts = time.strftime('%H:%M:%S')
        if self._listen.start():
            self.msg_list.insert(tk.END, f"{ts} – Listening server started")
            self.listen_button.config(text="Stop Listen")
        else:
            self._listen.stop()
            self.msg_list.insert(tk.END, f"{ts} – Listening server stopped")
            self.listen_button.config(text="Start Listen")
        self.msg_list.see(tk.END)

    def _on_restart(self):
        """Completely restart DNS, Zeroconf, HTTP & WS servers."""
        self.msg_list.insert(tk.END, f"{time.strftime('%H:%M:%S')} – Restarting Discovery…")
        self.msg_list.see(tk.END)
        self.service.restart()

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

    def _dispatch_to_plugins(cmd, debug=True):
        ctype = cmd.get("type")

        # 1) ignore purely discovery‐side events
        if ctype in ("zeroconf", "zeroconf_removed", "dns", "dns_sub"):
            return

        # 2) Global broadcasts: things that every checked device should see
        if ctype in ("recordStart", "recordStop", "fileName"):
            for name, var in ui.device_vars.items():
                if not var.get():
                    continue

                state = disco.device_states[name]
                ip, port = state.get("ip"), state.get("port")
                if not ip or not port:
                    continue

                enriched = { **cmd, "ip": ip, "port": port }
                # ← grab sub_ip (may be None) and always include it
                enriched["sub_ip"] = state.get("sub_ip")

                if debug:
                    print(f"[Dispatch→{name}](broadcast): {enriched}")

                plugin_mgr.handle(name, enriched)
            return

        # 3) Targeted commands: only go to cmd["device"]
        target = cmd.get("device")
        # fall back for your old timeouts if you still use 'value'
        if not target and ctype == "health_timeout":
            target = cmd.get("value")

        if not target or target not in ui.device_vars:
            return
        if not ui.device_vars[target].get():
            return

        state = disco.device_states[target]
        ip, port = state.get("ip"), state.get("port")
        if not ip or not port:
            return

        enriched = { **cmd, "ip": ip, "port": port }
        # ← **here again** pull sub_ip
        enriched["sub_ip"] = state.get("sub_ip")

        try:
            if debug:
                print(f"[DiscoveryService] Dispatching command to plugin {target}: {enriched}")
            plugin_mgr.handle(target, enriched)
        except Exception as e:
            if debug:
                print(f"[DiscoveryService] Plugin {target} failed to handle command: {cmd}")
            if enriched.get('type') == 'health' or enriched.get('type') == 'health_timeout':
                # if it's a health check, we still want to send the response
                disco._notify_command({
                    'type': 'health_response',
                    'device': target,
                    'value': False  # mark as unreachable
                })

    disco.subscribe_commands(_dispatch_to_plugins)

    root.mainloop()
