import json
import logging
import os
import platform
import socket
import sys
import time
from collections import deque
from typing import Any, Dict, Optional

try:
    import psutil
except ImportError:
    psutil = None

if __package__ in (None, ""):
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from client.process_monitor import TOP_N, collect_process_list, terminate_process
from common.command_auth import verify_process_command


logger = logging.getLogger(__name__)
PROCESS_LIST_CAPABILITY = "PROCESS_LIST_V1"
CONTROLLED_COMMANDS_CAPABILITY = "CONTROLLED_COMMANDS_V1"
PROCESS_MANAGEMENT_CAPABILITY = "PROCESS_MANAGEMENT_V1"
PROCESS_LIST_SOCKET_TIMEOUT_SECONDS = 10
PROCESSED_REQUEST_RETENTION_SECONDS = 300
MAX_PROCESSED_REQUEST_IDS = 2048
MAX_SERVER_FRAME_BYTES = 65536 + 512
CONTROLLED_COMMANDS = {
    "PING",
    "GET_INFO",
    "GET_PROCESS_LIST",
    "GET_NETWORK_INFO",
    "GET_PROCESSES",
    "TERMINATE_PROCESS",
}


class TCPClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8888, default_name: str = ""):
        self.host = host
        self.port = port
        self.default_name = default_name
        self._last_network_counters: tuple[int, int, int, int] | None = None
        self._last_network_sample_time: float | None = None
        self._processed_process_request_ids: deque[tuple[str, float]] = deque()
        self._processed_process_request_id_set: set[str] = set()

    def _build_message(self, action: str, payload: Optional[Dict[str, Any]] = None) -> str:
        payload = payload or {}
        if action == "REGISTER":
            name = str(payload.get("name") or self.default_name).strip()
            host = str(payload.get("host") or self.host).strip()
            port = payload.get("port") or self.port
            return (
                f"REGISTER|{name}|{host}|{port}|{PROCESS_LIST_CAPABILITY}"
                f"|{CONTROLLED_COMMANDS_CAPABILITY}|{PROCESS_MANAGEMENT_CAPABILITY}"
            )
        if action == "HEARTBEAT":
            name = str(payload.get("name") or self.default_name).strip()
            return f"HEARTBEAT|{name}"
        if action == "SYSTEM":
            name = str(payload.get("name") or self.default_name).strip()
            metrics = []
            for key in (
                "cpu",
                "ram",
                "disk",
                "network",
                "upload_bytes_per_sec",
                "download_bytes_per_sec",
                "packets_sent",
                "packets_recv",
            ):
                value = payload.get(key)
                if key in payload:
                    wire_key = {
                        "upload_bytes_per_sec": "UPLOAD_BPS",
                        "download_bytes_per_sec": "DOWNLOAD_BPS",
                        "packets_sent": "PACKETS_SENT",
                        "packets_recv": "PACKETS_RECV",
                    }.get(key, key.upper())
                    wire_value = "null" if value is None else str(value)
                    metrics.append(f"{wire_key}={wire_value}")
            message = f"SYSTEM|{name}"
            if metrics:
                message += "|" + "|".join(metrics)
            return message
        if action == "LOGOUT":
            name = str(payload.get("name") or self.default_name).strip()
            return f"LOGOUT|{name}"
        return str(action)

    def send(self, action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        operation = action if action in {"REGISTER", "HEARTBEAT", "SYSTEM", "LOGOUT"} else "unknown"
        try:
            with socket.create_connection((self.host, self.port), timeout=5) as sock:
                if action == "HEARTBEAT":
                    sock.settimeout(PROCESS_LIST_SOCKET_TIMEOUT_SECONDS)
                message = self._build_message(action, payload)
                logger.debug(
                    "Sending %s request to monitoring server at %s:%s.",
                    operation,
                    self.host,
                    self.port,
                )
                sock.sendall((message + "\n").encode("utf-8"))
                raw = self._read_line(sock)
                if raw is None:
                    logger.warning(
                        "No response to %s request from monitoring server.",
                        operation,
                    )
                    return {"status": "error", "message": "No response from server"}
                if not raw:
                    logger.warning(
                        "Empty response to %s request from monitoring server.",
                        operation,
                    )
                    return {"status": "error", "message": "Empty server response"}
                if action == "HEARTBEAT" and raw.startswith("COMMAND|"):
                    client_name = str(
                        (payload or {}).get("name") or self.default_name
                    ).strip()
                    return self._answer_server_command(
                        sock,
                        raw,
                        client_name,
                    )
                result = self._response_result(raw)
                if result["status"] == "ok":
                    logger.debug("Monitoring server accepted %s request.", operation)
                else:
                    logger.warning(
                        "Monitoring server rejected %s request.",
                        operation,
                    )
                return result
        except (OSError, socket.timeout) as exc:
            logger.warning(
                "TCP %s request failed (%s).",
                operation,
                type(exc).__name__,
            )
            return {"status": "error", "message": str(exc)}
        except ValueError as exc:
            logger.warning("Rejected oversized or malformed TCP response (%s).", type(exc).__name__)
            return {"status": "error", "message": "Malformed server response"}

    @staticmethod
    def _read_line(sock: socket.socket) -> Optional[str]:
        response = bytearray()
        while b"\n" not in response:
            data = sock.recv(4096)
            if not data:
                return response.decode("utf-8", errors="replace").strip() or None
            response.extend(data)
            if len(response) > MAX_SERVER_FRAME_BYTES:
                raise ValueError("Server response exceeded the maximum frame size.")
        line, _, _ = response.partition(b"\n")
        if len(line) > MAX_SERVER_FRAME_BYTES:
            raise ValueError("Server response exceeded the maximum frame size.")
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

    def _answer_server_command(
        self,
        sock: socket.socket,
        command: str,
        client_name: str,
    ) -> Dict[str, Any]:
        parts = command.split("|")
        if (
            len(parts) == 4
            and parts[1] == "GET_PROCESS_LIST"
            and parts[2].isalnum()
        ):
            return self._answer_legacy_process_list_command(
                sock,
                parts,
                command,
                client_name,
            )
        if (
            len(parts) not in {3, 4, 5}
            or not parts[1].isalnum()
            or len(parts[1]) > 64
            or parts[2] not in CONTROLLED_COMMANDS
            or (parts[2] == "GET_PROCESS_LIST" and len(parts) != 4)
            or (parts[2] in {"PING", "GET_INFO", "GET_NETWORK_INFO"} and len(parts) != 3)
            or (parts[2] == "GET_PROCESSES" and len(parts) != 4)
            or (parts[2] == "TERMINATE_PROCESS" and len(parts) != 5)
        ):
            logger.warning("Rejecting malformed or unsupported server command.")
            return {
                "status": "error",
                "raw": command,
                "message": "Unsupported server command",
            }

        request_id = parts[1]
        command_name = parts[2]
        signed_argument = ""
        signature = ""
        if command_name == "GET_PROCESSES":
            if len(parts) != 4:
                return self._unsupported_server_command(command)
            signature = parts[3]
        elif command_name == "TERMINATE_PROCESS":
            if len(parts) != 5:
                return self._unsupported_server_command(command)
            signed_argument = parts[3]
            signature = parts[4]
            if (
                not signed_argument.isascii()
                or not signed_argument.isdecimal()
                or not 1 <= int(signed_argument) <= 4_294_967_295
            ):
                return self._unsupported_server_command(command)
        elif command_name == "GET_PROCESS_LIST" and len(parts) == 4:
            pass
        elif len(parts) != 3:
            return self._unsupported_server_command(command)

        if command_name in {"GET_PROCESSES", "TERMINATE_PROCESS"}:
            token = os.environ.get("MONITOR_ADMIN_TOKEN", "")
            if not verify_process_command(
                token,
                request_id,
                client_name,
                command_name,
                signed_argument,
                signature,
            ):
                return self._send_command_error(
                    sock,
                    command,
                    request_id,
                    "UNAUTHORIZED",
                )
            if self._has_processed_request_id(request_id):
                return self._send_command_error(
                    sock,
                    command,
                    request_id,
                    "REPLAYED",
                )

        logger.info("Executing allowlisted server command %s.", command_name)
        try:
            if command_name == "PING":
                response_payload = "PONG"
            elif command_name == "GET_INFO":
                response_payload = json.dumps(
                    {
                        "client_name": client_name,
                        "hostname": socket.gethostname(),
                        "os": platform.system(),
                        "os_release": platform.release(),
                        "machine": platform.machine(),
                        "python_version": platform.python_version(),
                    },
                    separators=(",", ":"),
                )
            elif command_name == "GET_PROCESS_LIST":
                if not parts[3].isdigit() or not 1 <= int(parts[3]) <= TOP_N:
                    raise ValueError("Process-list limit is invalid.")
                process_list = collect_process_list(int(parts[3]))
                response_payload = json.dumps(process_list, separators=(",", ":"))
            elif command_name == "GET_PROCESSES":
                process_list = collect_process_list(TOP_N)
                response_payload = json.dumps(process_list, separators=(",", ":"))
            elif command_name == "TERMINATE_PROCESS":
                termination_result = terminate_process(int(signed_argument))
                response_payload = json.dumps(
                    termination_result,
                    separators=(",", ":"),
                )
            elif command_name == "GET_NETWORK_INFO":
                response_payload = json.dumps(
                    self._collect_network_info(),
                    separators=(",", ":"),
                )
            else:
                raise ValueError("Unsupported controlled command.")
            response = f"RESPONSE|{request_id}|{response_payload}"
        except (OSError, RuntimeError, ValueError) as error:
            logger.error(
                "Controlled command %s failed (%s).",
                command_name,
                type(error).__name__,
            )
            response = f"COMMAND_ERROR|{request_id}|UNAVAILABLE"

        if command_name in {"GET_PROCESSES", "TERMINATE_PROCESS"}:
            self._remember_processed_request_id(request_id)
        sock.sendall((response + "\n").encode("utf-8"))
        acknowledgement = self._read_line(sock)
        if acknowledgement is None:
            return {
                "status": "error",
                "raw": command,
                "message": "No acknowledgement for controlled command response",
                "command_status": "error",
            }
        ack_result = self._response_result(acknowledgement)
        if ack_result["status"] != "ok":
            logger.warning("Server rejected the controlled command response.")
        command_succeeded = (
            ack_result["status"] == "ok" and response.startswith("RESPONSE|")
        )
        if command_succeeded:
            logger.info("Controlled command %s completed.", command_name)
        else:
            logger.warning("Controlled command %s did not complete.", command_name)
        return {
            "status": "ok" if command_succeeded else "error",
            "raw": acknowledgement,
            "message": ack_result["message"],
            "command_status": (
                "complete" if command_succeeded else "error"
            ),
        }

    @staticmethod
    def _unsupported_server_command(command: str) -> Dict[str, Any]:
        logger.warning("Rejecting malformed or unsupported server command.")
        return {
            "status": "error",
            "raw": command,
            "message": "Unsupported server command",
        }

    def _send_command_error(
        self,
        sock: socket.socket,
        command: str,
        request_id: str,
        error_code: str,
    ) -> Dict[str, Any]:
        response = f"COMMAND_ERROR|{request_id}|{error_code}"
        sock.sendall((response + "\n").encode("utf-8"))
        acknowledgement = self._read_line(sock)
        if acknowledgement is None:
            return {
                "status": "error",
                "raw": command,
                "message": "No acknowledgement for rejected controlled command",
                "command_status": "error",
            }
        ack_result = self._response_result(acknowledgement)
        return {
            "status": "error",
            "raw": acknowledgement,
            "message": ack_result["message"],
            "command_status": "error",
        }

    def _has_processed_request_id(self, request_id: str) -> bool:
        self._prune_processed_request_ids()
        return request_id in self._processed_process_request_id_set

    def _remember_processed_request_id(self, request_id: str) -> None:
        self._prune_processed_request_ids()
        if request_id in self._processed_process_request_id_set:
            return
        self._processed_process_request_id_set.add(request_id)
        self._processed_process_request_ids.append((request_id, time.monotonic()))
        while len(self._processed_process_request_ids) > MAX_PROCESSED_REQUEST_IDS:
            expired_id, _ = self._processed_process_request_ids.popleft()
            self._processed_process_request_id_set.discard(expired_id)

    def _prune_processed_request_ids(self) -> None:
        cutoff = time.monotonic() - PROCESSED_REQUEST_RETENTION_SECONDS
        while (
            self._processed_process_request_ids
            and self._processed_process_request_ids[0][1] < cutoff
        ):
            expired_id, _ = self._processed_process_request_ids.popleft()
            self._processed_process_request_id_set.discard(expired_id)

    def _answer_legacy_process_list_command(
        self,
        sock: socket.socket,
        parts: list[str],
        command: str,
        client_name: str,
    ) -> Dict[str, Any]:
        if (
            len(parts) != 4
            or not parts[2].isalnum()
            or len(parts[2]) > 64
            or not parts[3].isdigit()
            or not 1 <= int(parts[3]) <= TOP_N
        ):
            logger.warning("Rejecting malformed legacy process-list command.")
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
                "Legacy process-list collection failed (%s).",
                type(error).__name__,
            )
            response = f"PROCESS_LIST_ERROR|{client_name}|{request_id}|UNAVAILABLE"

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
            logger.warning("Server rejected the legacy process-list response.")
        return {
            "status": ack_result["status"],
            "raw": acknowledgement,
            "message": ack_result["message"],
            "process_list_status": (
                "complete"
                if ack_result["status"] == "ok" and response.startswith("PROCESS_LIST|")
                else "error"
            ),
        }

    def _collect_network_info(self) -> dict[str, Any]:
        unavailable = {
            "bytes_sent": None,
            "bytes_recv": None,
            "packets_sent": None,
            "packets_recv": None,
            "upload_bytes_per_sec": None,
            "download_bytes_per_sec": None,
        }
        if psutil is None:
            return unavailable
        try:
            counters = psutil.net_io_counters()
        except psutil.Error as error:
            logger.warning(
                "Network statistics are unavailable (%s).",
                type(error).__name__,
            )
            return unavailable
        if counters is None:
            return unavailable
        current = (
            int(counters.bytes_sent),
            int(counters.bytes_recv),
            int(counters.packets_sent),
            int(counters.packets_recv),
        )
        sampled_at = time.monotonic()
        if self._last_network_counters is None or self._last_network_sample_time is None:
            upload_rate = 0.0
            download_rate = 0.0
        else:
            elapsed = max(0.001, sampled_at - self._last_network_sample_time)
            sent_delta = current[0] - self._last_network_counters[0]
            received_delta = current[1] - self._last_network_counters[1]
            if sent_delta < 0 or received_delta < 0:
                upload_rate = 0.0
                download_rate = 0.0
            else:
                upload_rate = round(sent_delta / elapsed, 2)
                download_rate = round(received_delta / elapsed, 2)
        self._last_network_counters = current
        self._last_network_sample_time = sampled_at
        return {
            "bytes_sent": current[0],
            "bytes_recv": current[1],
            "packets_sent": current[2],
            "packets_recv": current[3],
            "upload_bytes_per_sec": upload_rate,
            "download_bytes_per_sec": download_rate,
        }

    def register(self, name: str = "", host: str = None, port: int = None) -> Dict[str, Any]:
        client_name = name or self.default_name
        return self.send("REGISTER", {"name": client_name, "host": host or self.host, "port": port or self.port})

    def heartbeat(self, name: str = "") -> Dict[str, Any]:
        return self.send("HEARTBEAT", {"name": name or self.default_name})

    def send_metrics(
        self,
        name: str = "",
        cpu: float = 0,
        ram: float = 0,
        disk: float = 0,
        network: float = 0,
        upload_bytes_per_sec: float | None = None,
        download_bytes_per_sec: float | None = None,
        packets_sent: int | None = None,
        packets_recv: int | None = None,
    ) -> Dict[str, Any]:
        return self.send("SYSTEM", {
            "name": name or self.default_name,
            "cpu": cpu,
            "ram": ram,
            "disk": disk,
            "network": network,
            "upload_bytes_per_sec": upload_bytes_per_sec,
            "download_bytes_per_sec": download_bytes_per_sec,
            "packets_sent": packets_sent,
            "packets_recv": packets_recv,
        })

    def list_peers(self) -> Dict[str, Any]:
        return {"status": "deprecated", "message": "Peer discovery is not used in the monitoring server"}

    def ping(self, name: str = "") -> Dict[str, Any]:
        return self.send("HEARTBEAT", {"name": name or self.default_name or "probe"})

    def disconnect(self, name: str = "") -> Dict[str, Any]:
        return self.send("LOGOUT", {"name": name or self.default_name})
