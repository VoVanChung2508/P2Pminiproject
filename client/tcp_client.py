import json
import socket
from typing import Any, Dict, Optional

from shared.protocol import encode_message


class TCPClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9000):
        self.host = host
        self.port = port

    def send(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            message = encode_message(action, payload or {})
            sock.sendall(message.encode("utf-8"))
            data = sock.recv(4096)
            if not data:
                return {"status": "error", "message": "No response from server"}
            return json.loads(data.decode("utf-8", errors="ignore"))

    def register(self, name: str, host: str = None, port: int = None):
        return self.send("REGISTER", {"name": name, "host": host or self.host, "port": port or self.port})

    def list_peers(self) -> Dict[str, Any]:
        return self.send("LIST_PEERS")

    def ping(self) -> Dict[str, Any]:
        return self.send("PING")

    def disconnect(self) -> Dict[str, Any]:
        return self.send("DISCONNECT")
