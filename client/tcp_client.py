import os
import socket
import sys
from typing import Any, Dict, Optional

if __package__ in (None, ""):
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


class TCPClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8888, default_name: str = ""):
        self.host = host
        self.port = port
        self.default_name = default_name

    def _build_message(self, action: str, payload: Optional[Dict[str, Any]] = None) -> str:
        payload = payload or {}
        if action == "REGISTER":
            name = str(payload.get("name") or self.default_name).strip()
            host = str(payload.get("host") or self.host).strip()
            port = payload.get("port") or self.port
            return f"REGISTER|{name}|{host}|{port}"
        if action == "HEARTBEAT":
            name = str(payload.get("name") or self.default_name).strip()
            return f"HEARTBEAT|{name}"
        if action == "SYSTEM":
            name = str(payload.get("name") or self.default_name).strip()
            metrics = []
            for key in ("cpu", "ram", "disk", "network"):
                value = payload.get(key)
                if value is not None:
                    metrics.append(f"{key}={value}")
            message = f"SYSTEM|{name}"
            if metrics:
                message += "|" + "|".join(metrics)
            return message
        if action == "LOGOUT":
            name = str(payload.get("name") or self.default_name).strip()
            return f"LOGOUT|{name}"
        return str(action)

    def send(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            with socket.create_connection((self.host, self.port), timeout=5) as sock:
                message = self._build_message(action, payload)
                sock.sendall((message + "\n").encode("utf-8"))
                data = sock.recv(4096)
                if not data:
                    return {"status": "error", "message": "No response from server"}
                raw = data.decode("utf-8", errors="ignore").strip()
                if not raw:
                    return {"status": "error", "message": "Empty server response"}
                parts = raw.split("|", 1)
                status = parts[0].upper()
                return {"status": "ok" if status == "OK" else "error", "raw": raw, "message": parts[1] if len(parts) > 1 else raw}
        except (OSError, socket.timeout) as exc:
            return {"status": "error", "message": str(exc)}

    def register(self, name: str = "", host: str = None, port: int = None) -> Dict[str, Any]:
        client_name = name or self.default_name
        return self.send("REGISTER", {"name": client_name, "host": host or self.host, "port": port or self.port})

    def heartbeat(self, name: str = "") -> Dict[str, Any]:
        return self.send("HEARTBEAT", {"name": name or self.default_name})

    def send_metrics(self, name: str = "", cpu: float = 0, ram: float = 0, disk: float = 0, network: float = 0) -> Dict[str, Any]:
        return self.send("SYSTEM", {
            "name": name or self.default_name,
            "cpu": cpu,
            "ram": ram,
            "disk": disk,
            "network": network,
        })

    def list_peers(self) -> Dict[str, Any]:
        return {"status": "deprecated", "message": "Peer discovery is not used in the monitoring server"}

    def ping(self, name: str = "") -> Dict[str, Any]:
        return self.send("HEARTBEAT", {"name": name or self.default_name or "probe"})

    def disconnect(self, name: str = "") -> Dict[str, Any]:
        return self.send("LOGOUT", {"name": name or self.default_name})

