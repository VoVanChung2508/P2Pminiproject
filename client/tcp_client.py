import json
import logging
import os
import socket
import sys
from typing import Any, Dict, Optional

if __package__ in (None, ""):
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from client.process_monitor import TOP_N, collect_process_list


logger = logging.getLogger(__name__)
PROCESS_LIST_CAPABILITY = "PROCESS_LIST_V1"
PROCESS_LIST_SOCKET_TIMEOUT_SECONDS = 10


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
            return f"REGISTER|{name}|{host}|{port}|{PROCESS_LIST_CAPABILITY}"
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
                if action == "HEARTBEAT":
                    sock.settimeout(PROCESS_LIST_SOCKET_TIMEOUT_SECONDS)
                message = self._build_message(action, payload)
                sock.sendall((message + "\n").encode("utf-8"))
                raw = self._read_line(sock)
                if raw is None:
                    return {"status": "error", "message": "No response from server"}
                if not raw:
                    return {"status": "error", "message": "Empty server response"}
                if action == "HEARTBEAT" and raw.startswith("COMMAND|"):
                    client_name = str(
                        (payload or {}).get("name") or self.default_name
                    ).strip()
                    return self._answer_process_list_command(
                        sock,
                        raw,
                        client_name,
                    )
                return self._response_result(raw)
        except (OSError, socket.timeout) as exc:
            return {"status": "error", "message": str(exc)}

    @staticmethod
    def _read_line(sock: socket.socket) -> Optional[str]:
        response = bytearray()
        while b"\n" not in response:
            data = sock.recv(4096)
            if not data:
                return response.decode("utf-8", errors="replace").strip() or None
            response.extend(data)
        line, _, _remaining = response.partition(b"\n")
        return line.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _response_result(raw: str) -> Dict[str, Any]:
        parts = raw.split("|", 1)
        status = parts[0].upper()
        return {
            "status": "ok" if status == "OK" else "error",
            "raw": raw,
            "message": parts[1] if len(parts) > 1 else raw,
        }

    def _answer_process_list_command(
        self,
        sock: socket.socket,
        command: str,
        client_name: str,
    ) -> Dict[str, Any]:
        parts = command.split("|")
        if (
            len(parts) != 4
            or parts[1] != "GET_PROCESS_LIST"
            or not parts[2].isalnum()
            or len(parts[2]) > 64
            or not parts[3].isdigit()
            or not 1 <= int(parts[3]) <= TOP_N
        ):
            logger.warning("Ignoring malformed or unsupported server command.")
            return {"status": "error", "raw": command, "message": "Unsupported server command"}

        request_id = parts[2]
        limit = int(parts[3])
        try:
            process_list = collect_process_list(limit)
            response = (
                f"PROCESS_LIST|{client_name}|{request_id}|"
                f"{json.dumps(process_list, separators=(',', ':'))}"
            )
        except (OSError, RuntimeError, ValueError) as error:
            logger.error(
                "Process-list collection failed (%s).",
                type(error).__name__,
            )
            response = (
                f"PROCESS_LIST_ERROR|{client_name}|{request_id}|UNAVAILABLE"
            )

        sock.sendall((response + "\n").encode("utf-8"))
        acknowledgement = self._read_line(sock)
        if acknowledgement is None:
            return {
                "status": "error",
                "raw": command,
                "message": "No acknowledgement for process-list response",
                "process_list_status": "error",
            }
        ack_result = self._response_result(acknowledgement)
        if ack_result["status"] != "ok":
            logger.warning("Server rejected the process-list response.")
        return {
            "status": "ok",
            "raw": "OK|HEARTBEAT",
            "message": "HEARTBEAT",
            "process_list_status": (
                "complete"
                if ack_result["status"] == "ok" and response.startswith("PROCESS_LIST|")
                else "error"
            ),
        }

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
