import socket
import threading
import sys
from zeroconf import Zeroconf, ServiceInfo

# Example device that registers itself as a PineapplePipeline-capable service

SERVICE_TYPE = "_mocap._tcp.local."
SERVICE_NAME = "MyMocapDevice._mocap._tcp.local."  # change 'MyMocapDevice' to your device's hostname
SERVICE_PORT = 5000  # the port your device listens on
TXT_RECORD = {
    'model': 'ModelX',
    'version': '1.0'
}

class MocapDevice:
    def __init__(self, name, service_type, port, properties):
        self.zeroconf = Zeroconf()
        self.name = name
        self.service_type = service_type
        self.port = port
        self.properties = properties

        # Find local IP address
        self.ip = self._get_local_ip()
        self.info = ServiceInfo(
            type_=self.service_type,
            name=self.name,
            addresses=[socket.inet_aton(self.ip)],
            port=self.port,
            properties=self.properties,
            server=socket.gethostname() + ".local."
        )

    def _get_local_ip(self):
        # heuristic to get outbound IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # doesn't actually send data
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    def register(self):
        print(f"[+] Registering service {self.name} on {self.ip}:{self.port}")
        self.zeroconf.register_service(self.info)

    def unregister(self):
        print(f"[-] Unregistering service {self.name}")
        self.zeroconf.unregister_service(self.info)
        self.zeroconf.close()

    def serve(self):
        """Simple TCP server to illustrate device behavior."""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.bind((self.ip, self.port))
        server_sock.listen(1)
        print(f"[*] {self.name} listening for connections...")

        try:
            while True:
                conn, addr = server_sock.accept()
                print(f"[*] Connection from {addr}")
                data = conn.recv(1024)
                print(f"[*] Received: {data}")
                conn.sendall(b"ACK: " + data)
                conn.close()
        except KeyboardInterrupt:
            pass
        finally:
            server_sock.close()

if __name__ == '__main__':
    device = MocapDevice(
        name=SERVICE_NAME,
        service_type=SERVICE_TYPE,
        port=SERVICE_PORT,
        properties=TXT_RECORD
    )
    device.register()

    # Run the dummy server in a thread so we can catch KeyboardInterrupt
    server_thread = threading.Thread(target=device.serve, daemon=True)
    server_thread.start()

    try:
        print("Press Ctrl-C to exit and unregister service...")
        server_thread.join()
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        device.unregister()
