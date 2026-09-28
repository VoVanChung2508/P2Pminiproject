from __future__ import annotations

import hmac
import json
import logging
import math
import socket
import threading
import time
import uuid
from datetime import datetime
from os import environ
from typing import Any

from flask import Flask, jsonify, render_template_string, request
from common.database import DatabaseManager

HTTP_HOST = "0.0.0.0"
HTTP_PORT = int(environ.get("MONITOR_HTTP_PORT", "8081"))
TCP_HOST = "0.0.0.0"
TCP_PORT = int(environ.get("MONITOR_TCP_PORT", "8888"))
HEARTBEAT_TIMEOUT = 15
TCP_CLIENT_TIMEOUT = 30
SAMPLE_LIMIT = 120
ADMIN_TOKEN = environ.get("MONITOR_ADMIN_TOKEN", "")
PROCESS_LIST_CAPABILITY = "PROCESS_LIST_V1"
PROCESS_LIST_TOP_N = 20
PROCESS_LIST_REQUEST_TIMEOUT_SECONDS = 30
PROCESS_LIST_MAX_PAYLOAD_BYTES = 65536
PROCESS_LIST_MAX_TCP_FRAME_BYTES = PROCESS_LIST_MAX_PAYLOAD_BYTES + 512
PROCESS_LIST_MAX_TRACKED_CLIENTS = 1000
PROCESS_LIST_RESULT_RETENTION_SECONDS = 300

app = Flask(__name__)
logger = logging.getLogger(__name__)
state_lock = threading.RLock()
clients: dict[str, dict[str, Any]] = {}
disconnected_clients: set[str] = set()
process_requests: dict[str, dict[str, Any]] = {}

db_manager = DatabaseManager()


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")


def client_snapshot(client: dict[str, Any], runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(client)
    runtime = runtime or {}
    result["seconds_since_heartbeat"] = round(
        max(0.0, time.time() - runtime["last_seen_epoch"]), 1
    ) if runtime.get("last_seen_epoch") is not None else None
    return result


def parse_metric(value: str, name: str) -> float:
    number = float(value)
    if not 0 <= number <= 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return round(number, 1)


def register_client(
    name: str,
    ip: str,
    process_list_capable: bool = False,
) -> None:
    key = name.lower()
    if not db_manager.register_client(name, ip):
        raise RuntimeError("MySQL did not save the client registration.")
    with state_lock:
        disconnected_clients.discard(key)
        clients[key] = {
            "name": name,
            "ip": ip,
            "status": "ONLINE",
            "last_seen_epoch": time.time(),
            "process_list_capable": process_list_capable,
        }
    log(f"{name} registered from {ip}")


def update_system(name: str, metrics: dict[str, float]) -> bool:
    key = name.lower()
    with state_lock:
        client = clients.get(key)
        if client is None or key in disconnected_clients:
            return False
    if not db_manager.update_metrics(name, metrics):
        raise RuntimeError("MySQL did not save the client metrics.")
    for metric, limit in (("cpu", 80), ("ram", 80), ("disk", 90)):
        if metrics.get(metric, 0) > limit and not db_manager.add_alert(
            name, metric, metrics[metric], float(limit)
        ):
            raise RuntimeError("MySQL did not save the generated alert.")
    with state_lock:
        client = clients.get(key)
        if client is None or key in disconnected_clients:
            return False
        client["status"] = "ONLINE"
        client["last_seen_epoch"] = time.time()
    return True


def touch_client(name: str) -> bool:
    with state_lock:
        key = name.lower()
        client = clients.get(key)
        if not client or key in disconnected_clients:
            return False
    if not db_manager.update_heartbeat(name):
        raise RuntimeError("MySQL did not save the client heartbeat.")
    with state_lock:
        client = clients.get(key)
        if client and key not in disconnected_clients:
            client["status"] = "ONLINE"
            client["last_seen_epoch"] = time.time()
            return True
    return False


def is_client_disconnected(name: str) -> bool:
    with state_lock:
        return name.lower() in disconnected_clients


def _expire_process_request_locked(client_key: str) -> None:
    process_request = process_requests.get(client_key)
    if (
        process_request
        and process_request["status"] in {"pending", "delivered"}
        and time.time() >= process_request["expires_at"]
    ):
        process_request["status"] = "timeout"
        process_request["processes"] = None


def _prune_process_requests_locked(now: float) -> None:
    for client_key, process_request in tuple(process_requests.items()):
        if (
            process_request["status"] in {"pending", "delivered"}
            and now >= process_request["expires_at"]
        ):
            process_request["status"] = "timeout"
            process_request["processes"] = None
        elif (
            process_request["status"] not in {"pending", "delivered"}
            and now
            >= process_request.get(
                "completed_at",
                process_request["requested_at"],
            )
            + PROCESS_LIST_RESULT_RETENTION_SECONDS
        ):
            del process_requests[client_key]


def _queue_process_request(name: str) -> tuple[dict[str, Any] | None, str | None]:
    key = name.lower()
    with state_lock:
        now = time.time()
        _prune_process_requests_locked(now)
        client = clients.get(key)
        if (
            client is None
            or client["status"] != "ONLINE"
            or key in disconnected_clients
        ):
            return None, "Client is not registered or is offline."
        if not client.get("process_list_capable", False):
            return None, "Client does not support process-list requests."
        _expire_process_request_locked(key)
        existing = process_requests.get(key)
        if existing and existing["status"] in {"pending", "delivered"}:
            return None, "A process-list request is already pending."
        if key not in process_requests and len(process_requests) >= PROCESS_LIST_MAX_TRACKED_CLIENTS:
            return None, "Process-list request capacity is full."

        process_request = {
            "client": client["name"],
            "request_id": uuid.uuid4().hex,
            "status": "pending",
            "requested_at": now,
            "expires_at": now + PROCESS_LIST_REQUEST_TIMEOUT_SECONDS,
            "processes": None,
        }
        process_requests[key] = process_request
        return dict(process_request), None


def _next_process_command(
    name: str,
    address: tuple[str, int],
) -> str | None:
    key = name.lower()
    with state_lock:
        client = clients.get(key)
        process_request = process_requests.get(key)
        if (
            client is None
            or not client.get("process_list_capable", False)
            or process_request is None
            or process_request["status"] != "pending"
        ):
            return None
        _expire_process_request_locked(key)
        if process_request["status"] != "pending":
            return None
        process_request["status"] = "delivered"
        process_request["delivery_address"] = address
        return (
            f"COMMAND|GET_PROCESS_LIST|{process_request['request_id']}|"
            f"{PROCESS_LIST_TOP_N}"
        )


def _validate_process_list(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > min(limit, PROCESS_LIST_TOP_N):
        raise ValueError("Process list must be an array within the requested limit.")

    fields = {
        "pid",
        "name",
        "username",
        "cpu_percent",
        "memory_percent",
        "status",
    }
    validated = []
    seen_pids: set[int] = set()
    for process in value:
        if not isinstance(process, dict) or set(process) != fields:
            raise ValueError("Process record fields are invalid.")
        pid = process["pid"]
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("Process ID is invalid.")
        if pid in seen_pids:
            raise ValueError("Process ID is duplicated.")
        seen_pids.add(pid)
        if (
            not isinstance(process["name"], str)
            or not process["name"]
            or len(process["name"]) > 256
        ):
            raise ValueError("Process name is invalid.")
        for optional_text in ("username", "status"):
            text = process[optional_text]
            if text is not None and (
                not isinstance(text, str) or len(text) > (256 if optional_text == "username" else 64)
            ):
                raise ValueError(f"Process {optional_text} is invalid.")
        for percentage_field in ("cpu_percent", "memory_percent"):
            percentage = process[percentage_field]
            if percentage is not None:
                if isinstance(percentage, bool) or not isinstance(
                    percentage,
                    (int, float),
                ):
                    raise ValueError(f"Process {percentage_field} is invalid.")
                try:
                    numeric_percentage = float(percentage)
                except OverflowError as error:
                    raise ValueError(
                        f"Process {percentage_field} is invalid."
                    ) from error
                if (
                    not math.isfinite(numeric_percentage)
                    or not 0 <= numeric_percentage <= 100
                ):
                    raise ValueError(f"Process {percentage_field} is invalid.")
        validated.append(dict(process))
    return validated


def _accept_process_list(
    name: str,
    request_id: str,
    payload: str,
    address: tuple[str, int],
) -> str:
    key = name.lower()
    with state_lock:
        process_request = process_requests.get(key)
        if (
            key not in clients
            or clients[key]["status"] != "ONLINE"
            or key in disconnected_clients
            or not clients[key].get("process_list_capable", False)
            or process_request is None
            or process_request["request_id"] != request_id
            or process_request["status"] != "delivered"
            or process_request["delivery_address"] != address
        ):
            return "ERROR|INVALID_PROCESS_LIST_REQUEST"

        _expire_process_request_locked(key)
        if process_request["status"] != "delivered":
            return "ERROR|PROCESS_LIST_TIMEOUT"

    if len(payload.encode("utf-8")) > PROCESS_LIST_MAX_PAYLOAD_BYTES:
        with state_lock:
            current = process_requests.get(key)
            if current and current["request_id"] == request_id:
                current["status"] = "error"
                current["processes"] = None
                current["completed_at"] = time.time()
        logger.warning(
            "Rejected oversized process-list reply from registered client %s.",
            name,
        )
        return "ERROR|MALFORMED_PROCESS_LIST"

    try:
        processes = _validate_process_list(
            json.loads(payload),
            PROCESS_LIST_TOP_N,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        with state_lock:
            current = process_requests.get(key)
            if current and current["request_id"] == request_id:
                current["status"] = "error"
                current["processes"] = None
                current["completed_at"] = time.time()
        logger.warning(
            "Rejected malformed process-list reply from registered client %s (%s).",
            name,
            type(error).__name__,
        )
        return "ERROR|MALFORMED_PROCESS_LIST"

    with state_lock:
        current = process_requests.get(key)
        if (
            current is None
            or key not in clients
            or clients[key]["status"] != "ONLINE"
            or key in disconnected_clients
            or current["request_id"] != request_id
            or current["status"] != "delivered"
            or current["delivery_address"] != address
        ):
            return "ERROR|INVALID_PROCESS_LIST_REQUEST"
        current["status"] = "complete"
        current["processes"] = processes
        current["completed_at"] = time.time()
    logger.info("Received %d process records from client %s.", len(processes), name)
    return "OK|PROCESS_LIST"


def disconnect_client(name: str) -> bool:
    key = name.lower()
    with state_lock:
        client = clients.get(key)
        if client is None:
            return False
    if not db_manager.update_status(client["name"], "OFFLINE"):
        raise RuntimeError("MySQL did not save the client status.")
    with state_lock:
        disconnected_clients.add(key)
        client["status"] = "OFFLINE"
    log(f"{client['name']} disconnected by server administrator")
    return True


def mark_offline_clients() -> None:
    while True:
        time.sleep(2)
        current_time = time.time()
        with state_lock:
            for client in clients.values():
                if current_time - client["last_seen_epoch"] > HEARTBEAT_TIMEOUT:
                    if client["status"] != "OFFLINE":
                        log(f"{client['name']} -> OFFLINE")
                        if not db_manager.update_status(client['name'], "OFFLINE"):
                            log("ERROR: Could not save OFFLINE status to MySQL.")
                    client["status"] = "OFFLINE"


def handle_message(message: str, address: tuple[str, int]) -> str:
    process_parts = message.strip().split("|", 3)
    process_command = process_parts[0].upper()
    if process_command == "PROCESS_LIST":
        if len(process_parts) != 4:
            return "ERROR|MALFORMED_PROCESS_LIST"
        return _accept_process_list(
            process_parts[1],
            process_parts[2],
            process_parts[3],
            address,
        )
    if process_command == "PROCESS_LIST_ERROR":
        if len(process_parts) != 4:
            return "ERROR|MALFORMED_PROCESS_LIST"
        name, request_id, error_code = process_parts[1:]
        if error_code not in {"UNAVAILABLE", "FAILED"}:
            return "ERROR|MALFORMED_PROCESS_LIST"
        key = name.lower()
        with state_lock:
            process_request = process_requests.get(key)
            if (
                key not in clients
                or clients[key]["status"] != "ONLINE"
                or key in disconnected_clients
                or not clients[key].get("process_list_capable", False)
                or process_request is None
                or process_request["request_id"] != request_id
                or process_request["status"] != "delivered"
                or process_request["delivery_address"] != address
            ):
                return "ERROR|INVALID_PROCESS_LIST_REQUEST"
            _expire_process_request_locked(key)
            if process_request["status"] != "delivered":
                return "ERROR|PROCESS_LIST_TIMEOUT"
            process_request["status"] = "error"
            process_request["processes"] = None
            process_request["completed_at"] = time.time()
        logger.warning(
            "Client %s could not provide its requested process list (%s).",
            name,
            error_code,
        )
        return "OK|PROCESS_LIST"

    parts = [part.strip() for part in message.strip().split("|")]
    if not parts:
        return "ERROR|Empty message"
    command = parts[0].upper()
    try:
        if command == "REGISTER" and len(parts) >= 2:
            if not parts[1]:
                return "ERROR|Client name is required"
            register_client(
                parts[1],
                address[0],
                PROCESS_LIST_CAPABILITY in parts[2:],
            )
            return "OK|REGISTERED"
        if len(parts) >= 2 and is_client_disconnected(parts[1]):
            return "ERROR|DISCONNECTED"
        if command == "HEARTBEAT" and len(parts) >= 2:
            if not touch_client(parts[1]):
                reason = "DISCONNECTED" if is_client_disconnected(parts[1]) else "NOT_REGISTERED"
                return f"ERROR|{reason}"
            process_command = _next_process_command(parts[1], address)
            if process_command is not None:
                return process_command
            return "OK|HEARTBEAT"
        if command == "SYSTEM" and len(parts) >= 2:
            metrics: dict[str, float] = {}
            for item in parts[2:]:
                key, value = item.split("=", 1)
                metric_name = key.lower()
                if metric_name in {"cpu", "ram", "disk", "network"}:
                    metrics[metric_name] = parse_metric(value.rstrip("%"), metric_name)
            if not metrics:
                return "ERROR|No metrics supplied"
            if not update_system(parts[1], metrics):
                reason = "DISCONNECTED" if is_client_disconnected(parts[1]) else "NOT_REGISTERED"
                return f"ERROR|{reason}"
            return "OK|SYSTEM"
        if command == "LOGOUT" and len(parts) >= 2:
            with state_lock:
                client = clients.get(parts[1].lower())
            if client:
                if not db_manager.update_status(parts[1], "OFFLINE"):
                    raise RuntimeError("MySQL did not save the client logout.")
                with state_lock:
                    client["status"] = "OFFLINE"
            return "OK|LOGOUT"
        return "ERROR|Unsupported message"
    except (ValueError, IndexError) as exc:

        return f"ERROR|{exc}"
    except RuntimeError as exc:
        log(f"Database operation failed: {exc}")
        return "ERROR|DATABASE_UNAVAILABLE"


def tcp_client_session(connection: socket.socket, address: tuple[str, int]) -> None:
    try:
        connection.settimeout(TCP_CLIENT_TIMEOUT)
        buffer = ""
        while True:
            try:
                data = connection.recv(4096)
            except socket.timeout:
                logger.warning(
                    "TCP client session timed out from %s:%s; closing connection.",
                    address[0],
                    address[1],
                )
                break
            except ConnectionResetError:
                logger.info(
                    "TCP client reset connection from %s:%s.",
                    address[0],
                    address[1],
                )
                break
            except OSError:
                logger.exception(
                    "TCP receive failed for client at %s:%s.",
                    address[0],
                    address[1],
                )
                return
            if not data:
                logger.debug(
                    "TCP client closed connection from %s:%s.",
                    address[0],
                    address[1],
                )
                break
            buffer += data.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if len(line.encode("utf-8")) > PROCESS_LIST_MAX_TCP_FRAME_BYTES:
                    logger.warning(
                        "Rejected oversized TCP message from %s:%s.",
                        address[0],
                        address[1],
                    )
                    return
                if line.strip():
                    response = handle_message(line, address)
                    try:
                        connection.sendall((response + "\n").encode("utf-8"))
                    except BrokenPipeError:
                        logger.info(
                            "TCP client closed before response could be sent to %s:%s.",
                            address[0],
                            address[1],
                        )
                        return
                    except ConnectionResetError:
                        logger.info(
                            "TCP client reset connection while sending to %s:%s.",
                            address[0],
                            address[1],
                        )
                        return
                    except OSError:
                        logger.exception(
                            "TCP send failed for client at %s:%s.",
                            address[0],
                            address[1],
                        )
                        return
            if len(buffer.encode("utf-8")) > PROCESS_LIST_MAX_TCP_FRAME_BYTES:
                logger.warning(
                    "TCP message exceeded maximum size from %s:%s.",
                    address[0],
                    address[1],
                )
                return
    except OSError:
        logger.exception(
            "TCP client session failed for client at %s:%s.",
            address[0],
            address[1],
        )
    finally:
        try:
            connection.close()
        except OSError:
            logger.exception(
                "Could not close TCP client connection from %s:%s.",
                address[0],
                address[1],
            )


def tcp_server() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((TCP_HOST, TCP_PORT))
        server_socket.listen(50)
        log(f"Monitoring TCP server listening on {TCP_PORT}")
        while True:
            try:
                connection, address = server_socket.accept()
            except OSError:
                if server_socket.fileno() == -1:
                    logger.info("TCP listener socket closed; stopping accept loop.")
                else:
                    logger.exception("TCP accept failed; stopping accept loop.")
                break
            try:
                threading.Thread(
                    target=tcp_client_session, args=(connection, address), daemon=True
                ).start()
            except Exception:
                logger.exception(
                    "Could not start TCP handler for client at %s:%s.",
                    address[0],
                    address[1],
                )
                try:
                    connection.close()
                except OSError:
                    logger.exception(
                        "Could not close unhandled TCP connection from %s:%s.",
                        address[0],
                        address[1],
                    )


def is_port_available(port: int, host: str = "0.0.0.0") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
            return True
    except OSError:
        return False


def find_available_port(preferred: int, host: str = "0.0.0.0", max_attempts: int = 50) -> int:
    for p in range(preferred, preferred + max_attempts):
        if is_port_available(p, host):
            return p
    return preferred


def ensure_ports_available() -> None:
    global HTTP_PORT, TCP_PORT
    for service, port_var_name in (("TCP", "TCP_PORT"), ("HTTP", "HTTP_PORT")):
        port = globals()[port_var_name]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError as exc:
                env_key = f"MONITOR_{service}_PORT"
                free_port = find_available_port(8081 if port == 8080 else port + 1)
                if env_key not in environ:
                    log(f"Port {port} for {service} is occupied ({exc}). Auto-switching to port {free_port}.")
                    globals()[port_var_name] = free_port
                    continue
                raise SystemExit(
                    f"Cannot start {service} service on port {port}: {exc}. "
                    f"Set {env_key} to a free port (e.g., {free_port})."
                ) from exc


@app.route("/")
def dashboard():
    return render_template_string(
        DASHBOARD_HTML,
        tcp_port=TCP_PORT,
        heartbeat_timeout=HEARTBEAT_TIMEOUT,
        admin_disconnect_enabled=bool(ADMIN_TOKEN),
    )


@app.route("/api/health")
def api_health():
    return jsonify({
        "status": "ok",
        "project": "Network Monitoring System",
        "tcp_port": TCP_PORT,
        "http_port": HTTP_PORT,
        "storage": "mysql" if db_manager.is_connected else "unavailable",
        "admin_disconnect_enabled": bool(ADMIN_TOKEN),
    })


@app.route("/api/clients")
def api_clients():
    try:
        stored_clients = db_manager.get_clients()
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    with state_lock:
        result = [
            client_snapshot(
                client,
                clients.get(str(client["name"]).lower()),
            )
            for client in stored_clients
        ]
    return jsonify({"clients": result})


@app.route("/api/clients/<name>/history")
def api_client_history(name: str):
    try:
        samples = db_manager.get_history(name, SAMPLE_LIMIT)
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    return jsonify({"client": name, "samples": samples})


@app.route("/api/alerts")
def api_alerts():
    try:
        stored_alerts = db_manager.get_alerts(100)
    except RuntimeError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503
    return jsonify({"alerts": stored_alerts})


def _admin_token_error() -> tuple[str, int] | None:
    if not ADMIN_TOKEN:
        return "Server admin token is not configured", 503
    supplied_token = request.headers.get("X-Admin-Token", "")
    if not hmac.compare_digest(supplied_token, ADMIN_TOKEN):
        return "Invalid admin token", 401
    return None


@app.route("/api/clients/<name>/process-list", methods=["GET", "POST"])
def api_process_list(name: str):
    auth_error = _admin_token_error()
    if auth_error is not None:
        message, status_code = auth_error
        return jsonify({"status": "error", "message": message}), status_code

    key = name.lower()
    if request.method == "POST":
        process_request, error = _queue_process_request(name)
        if error is not None:
            with state_lock:
                client = clients.get(key)
                if client is None:
                    error_code = "CLIENT_NOT_FOUND"
                elif (
                    client["status"] != "ONLINE"
                    or key in disconnected_clients
                ):
                    error_code = "CLIENT_OFFLINE"
                elif not client.get("process_list_capable", False):
                    error_code = "PROCESS_LIST_UNSUPPORTED"
                else:
                    error_code = "PROCESS_LIST_PENDING"
            status_code = (
                404
                if error_code == "CLIENT_NOT_FOUND"
                else 503
                if error_code == "PROCESS_LIST_CAPACITY"
                else 409
            )
            return jsonify(
                {
                    "status": "error",
                    "message": error,
                    "code": error_code,
                }
            ), status_code
        logger.info("Queued process-list request for registered client %s.", name)
        return jsonify({"status": "ok", "request": process_request}), 202

    with state_lock:
        _expire_process_request_locked(key)
        _prune_process_requests_locked(time.time())
        process_request = process_requests.get(key)
        if process_request is None:
            return jsonify(
                {"status": "error", "message": "No process-list request found."}
            ), 404
        result = dict(process_request)
        result.pop("expires_at", None)
    return jsonify({"status": "ok", "request": result})


@app.post("/api/clients/<name>/disconnect")
def api_disconnect_client(name: str):
    auth_error = _admin_token_error()
    if auth_error is not None:
        message, status_code = auth_error
        return jsonify({"status": "error", "message": message}), status_code
    if not disconnect_client(name):
        return jsonify({"status": "error", "message": "Client not found"}), 404
    return jsonify({"status": "ok", "message": f"Client {name} disconnected"})


DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Network Monitoring System</title>
<style>
:root{--bg:#090d16;--card:#111927;--border:#1e293b;--primary:#38bdf8;--success:#22c55e;--danger:#ef4444;--warning:#f59e0b;--text:#f1f5f9;--subtext:#94a3b8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 'Segoe UI',system-ui,-apple-system,sans-serif}
.wrap{max-width:1300px;margin:24px auto;padding:0 20px}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:18px;margin-bottom:20px}
.title-group h1{margin:0;font-size:22px;letter-spacing:1px;color:var(--primary);font-weight:700}
.title-group .sub{margin:4px 0 0;color:var(--subtext);font-size:13px}
.meta-badges{display:flex;gap:10px}
.badge{background:#1e293b;border:1px solid #334155;padding:5px 12px;border-radius:20px;font-size:12px;color:#cbd5e1}
.stats-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:20px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;display:flex;flex-direction:column}
.stat-card .label{font-size:12px;color:var(--subtext);text-transform:uppercase;font-weight:600}
.stat-card .val{font-size:26px;font-weight:700;margin-top:6px;color:var(--primary)}
.chart-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:20px 0}
.chart-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.chart-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.chart-head h3{margin:0;font-size:16px}
.chart-value{display:inline-block;padding:5px 10px;border-radius:999px;background:rgba(56,189,248,.10);color:#bae6fd;border:1px solid rgba(56,189,248,.35);font-weight:700}
.chart-card canvas{display:block;width:100%;height:180px;border-radius:8px;border:1px solid #1f2937;background:linear-gradient(180deg,#0f172a,#020817)}
.main-grid{display:grid;grid-template-columns:2.5fr 1fr;gap:20px}
.panel{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px}
.panel h2{font-size:16px;margin:0 0 14px;color:#e2e8f0;display:flex;align-items:center;justify-content:space-between}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:12px 10px;border-bottom:1px solid var(--border)}
th{color:var(--subtext);font-size:11px;text-transform:uppercase;letter-spacing:0.5px}
.client-row{cursor:pointer;transition:background .2s ease}
.client-row:hover{background:rgba(56,189,248,.05)}
.client-row.selected{background:rgba(56,189,248,.12)}
.online-tag{color:var(--success);font-weight:600;display:inline-flex;align-items:center;gap:6px}
.online-tag::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--success)}
.offline-tag{color:var(--danger);font-weight:600;display:inline-flex;align-items:center;gap:6px}
.offline-tag::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--danger)}
.bar-wrap{display:flex;align-items:center;gap:8px}
.bar{height:6px;background:#1e293b;border-radius:4px;flex:1;overflow:hidden}
.fill{height:100%;background:var(--primary);border-radius:4px;transition:width 0.4s ease}
.fill.warn{background:var(--warning)}
.fill.high{background:var(--danger)}
.alert-item{border-left:3px solid var(--warning);background:rgba(245,158,11,0.08);padding:10px 12px;border-radius:0 6px 6px 0;margin-bottom:8px;font-size:13px}
.alert-time{font-size:11px;color:var(--subtext);margin-top:4px}
.disconnect-button{background:#7f1d1d;color:#fee2e2;border:1px solid #b91c1c;padding:5px 9px;border-radius:6px;cursor:pointer}
.disconnect-button:disabled{opacity:.45;cursor:not-allowed}
.empty{color:var(--subtext);text-align:center;padding:24px 0}
@media(max-width:900px){.main-grid{grid-template-columns:1fr}.stats-grid{grid-template-columns:1fr}.chart-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="title-group">
      <h1>NETWORK MONITORING SYSTEM</h1>
      <p class="sub">Hệ thống Giám sát Mạng & Thiết bị Phân tán theo thời gian thực</p>
    </div>
    <div class="meta-badges">
      <span class="badge">TCP: {{ tcp_port }}</span>
      <span class="badge">Heartbeat: {{ heartbeat_timeout }}s</span>
      <span class="badge">Admin: {{ 'enabled' if admin_disconnect_enabled else 'disabled' }}</span>
      <span class="badge" id="last-sync">Sync: Đang tải...</span>
    </div>
  </header>

  <div class="stats-grid">
    <div class="stat-card"><span class="label">Tổng số Nodes</span><span class="val" id="stat-total">0</span></div>
    <div class="stat-card"><span class="label">Nodes Đang Online</span><span class="val" style="color:var(--success)" id="stat-online">0</span></div>
    <div class="stat-card"><span class="label">Cảnh báo Vượt ngưỡng</span><span class="val" style="color:var(--warning)" id="stat-alerts">0</span></div>
  </div>

  <div class="chart-grid">
    <div class="chart-card">
      <div class="chart-head"><h3>CPU</h3><span class="chart-value" id="cpu-value">0%</span></div>
      <canvas id="cpu-chart" width="320" height="180"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-head"><h3>RAM</h3><span class="chart-value" id="ram-value">0%</span></div>
      <canvas id="ram-chart" width="320" height="180"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-head"><h3>Network Traffic</h3><span class="chart-value" id="network-value">0%</span></div>
      <canvas id="network-chart" width="320" height="180"></canvas>
    </div>
  </div>

  <div class="main-grid">
    <section class="panel">
      <h2><span>Danh sách Thiết bị / Nodes</span><span style="font-size:12px;color:var(--subtext);font-weight:normal" id="nodes-count"></span></h2>
      <div style="overflow-x:auto">
        <table>
          <thead>
            <tr>
              <th>Client</th>
              <th>IP Address</th>
              <th>CPU</th>
              <th>RAM</th>
              <th>Disk</th>
              <th>Network</th>
              <th>Last Seen</th>
              <th>Trạng thái</th>
              <th>Thao tác</th>
            </tr>
          </thead>
          <tbody id="clients">
            <tr><td colspan="9" class="empty">Đang kết nối tới máy chủ...</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <h2><span>Nhật ký Cảnh báo (Alerts)</span></h2>
      <div id="alerts"><div class="empty">Không có cảnh báo</div></div>
    </section>
  </div>
</div>

<script>
const chartSettings = {
  cpu: { color: '#38bdf8', valueEl: 'cpu-value', canvasId: 'cpu-chart' },
  ram: { color: '#22c55e', valueEl: 'ram-value', canvasId: 'ram-chart' },
  network: { color: '#a78bfa', valueEl: 'network-value', canvasId: 'network-chart' }
};
let selectedClientName = null;
const adminDisconnectEnabled = {{ 'true' if admin_disconnect_enabled else 'false' }};
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[char]));

function renderLastSeen(value) {
  return value == null || value === '' ? '—' : escapeHtml(value);
}

function renderMetric(val) {
  const num = parseFloat(val) || 0;
  const cls = num > 90 ? 'high' : (num > 75 ? 'warn' : '');
  return `<div class="bar-wrap"><span>${num}%</span><div class="bar"><div class="fill ${cls}" style="width:${Math.min(100, num)}%"></div></div></div>`;
}

function normalizeHistory(samples = []) {
  return samples.slice(-30).map((sample) => ({
    cpu: Number(sample.cpu || 0),
    ram: Number(sample.ram || 0),
    network: Number(sample.network || 0),
    timestamp: sample.timestamp || Date.now()
  }));
}

function drawChart(canvasId, values, color) {
  const canvas = document.getElementById(canvasId);
  const ctx = canvas.getContext('2d');
  const width = canvas.width = Math.max(canvas.clientWidth * 2, 320);
  const height = canvas.height = 180 * 2;
  const pad = 18;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = '#020817';
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = 'rgba(148,163,184,0.18)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad + ((height - pad * 2) / 4) * i;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(width - pad, y);
    ctx.stroke();
  }

  if (!values || values.length === 0) {
    ctx.fillStyle = '#94a3b8';
    ctx.font = '14px Segoe UI';
    ctx.fillText('Chưa có dữ liệu', pad + 8, height / 2);
    return;
  }

  const data = values.map(v => Number(v) || 0);
  const maxVal = Math.max(100, ...data, 10);
  const stepX = (width - pad * 2) / Math.max(data.length - 1, 1);

  ctx.beginPath();
  data.forEach((value, index) => {
    const x = pad + index * stepX;
    const y = height - pad - ((value / maxVal) * (height - pad * 2));
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 2.5;
  ctx.shadowColor = color;
  ctx.shadowBlur = 10;
  ctx.stroke();
  ctx.shadowBlur = 0;
}

function renderCharts(historySamples) {
  const history = normalizeHistory(historySamples);
  const cpuValues = history.map(item => item.cpu);
  const ramValues = history.map(item => item.ram);
  const networkValues = history.map(item => item.network);

  const cpuCurrent = cpuValues.length ? cpuValues[cpuValues.length - 1] : 0;
  const ramCurrent = ramValues.length ? ramValues[ramValues.length - 1] : 0;
  const networkCurrent = networkValues.length ? networkValues[networkValues.length - 1] : 0;

  document.getElementById('cpu-value').textContent = `${cpuCurrent.toFixed(0)}%`;
  document.getElementById('ram-value').textContent = `${ramCurrent.toFixed(0)}%`;
  document.getElementById('network-value').textContent = `${networkCurrent.toFixed(0)}%`;

  drawChart('cpu-chart', cpuValues, '#38bdf8');
  drawChart('ram-chart', ramValues, '#22c55e');
  drawChart('network-chart', networkValues, '#a78bfa');
}

async function fetchClientHistory(clientName) {
  if (!clientName) return [];
  try {
    const res = await fetch(`/api/clients/${encodeURIComponent(clientName)}/history`);
    if (!res.ok) return [];
    const data = await res.json();
    return data.samples || [];
  } catch (error) {
    return [];
  }
}

function updateSelection(clients) {
  if (!clients || clients.length === 0) {
    selectedClientName = null;
    return;
  }

  const available = clients.map(c => c.name);
  if (!selectedClientName || !available.includes(selectedClientName)) {
    const preferredOnline = clients.find(c => c.status === 'ONLINE');
    selectedClientName = preferredOnline ? preferredOnline.name : clients[0].name;
  }
}

async function refresh() {
  try {
    const [clientsRes, alertsRes] = await Promise.all([
      fetch('/api/clients'),
      fetch('/api/alerts')
    ]);
    const clientsData = (await clientsRes.json()).clients || [];
    const alertsData = (await alertsRes.json()).alerts || [];

    updateSelection(clientsData);

    const onlineCount = clientsData.filter(c => c.status === 'ONLINE').length;
    document.getElementById('stat-total').textContent = clientsData.length;
    document.getElementById('stat-online').textContent = onlineCount;
    document.getElementById('stat-alerts').textContent = alertsData.length;
    document.getElementById('nodes-count').textContent = `${onlineCount}/${clientsData.length} online`;

    const clientsTbody = document.getElementById('clients');
    if (clientsData.length === 0) {
      clientsTbody.innerHTML = '<tr><td colspan="9" class="empty">Chưa có thiết bị nào đăng ký</td></tr>';
      renderCharts([]);
    } else {
      clientsTbody.innerHTML = clientsData.map(c => `
        <tr class="client-row ${selectedClientName === c.name ? 'selected' : ''}" data-client-name="${escapeHtml(c.name)}">
          <td><strong>${escapeHtml(c.name)}</strong></td>
          <td><code>${escapeHtml(c.ip)}</code></td>
          <td style="min-width:110px">${renderMetric(c.cpu)}</td>
          <td style="min-width:110px">${renderMetric(c.ram)}</td>
          <td style="min-width:110px">${renderMetric(c.disk)}</td>
          <td style="min-width:110px">${renderMetric(c.network)}</td>
          <td>${renderLastSeen(c.last_seen)}</td>
          <td><span class="${c.status === 'ONLINE' ? 'online-tag' : 'offline-tag'}">${escapeHtml(c.status)}</span></td>
          <td><button class="disconnect-button" type="button" ${c.status !== 'ONLINE' || !adminDisconnectEnabled ? 'disabled' : ''}>Ngắt</button></td>
        </tr>
      `).join('');
      clientsTbody.querySelectorAll('tr[data-client-name]').forEach(row => {
        row.addEventListener('click', () => selectClient(row.dataset.clientName));
        row.querySelector('.disconnect-button').addEventListener('click', event => {
          event.stopPropagation();
          disconnectClient(row.dataset.clientName);
        });
      });

      const selectedHistory = await fetchClientHistory(selectedClientName);
      renderCharts(selectedHistory);
    }

    const alertsBox = document.getElementById('alerts');
    if (alertsData.length === 0) {
      alertsBox.innerHTML = '<div class="empty">Hệ thống bình thường, không có cảnh báo</div>';
    } else {
      alertsBox.innerHTML = alertsData.slice(0, 10).map(a => `
        <div class="alert-item">
          <div><strong>${escapeHtml(a.client)}</strong>: Chỉ số ${escapeHtml(a.metric)} vượt ngưỡng (<b>${escapeHtml(a.value)}%</b> &gt; ${escapeHtml(a.limit)}%)</div>
          <div class="alert-time">${escapeHtml(a.timestamp)}</div>
        </div>
      `).join('');
    }

    const now = new Date();
    document.getElementById('last-sync').textContent = 'Sync: ' + now.toLocaleTimeString();
  } catch (err) {
    document.getElementById('last-sync').textContent = 'Mất kết nối API';
  }
}

function selectClient(clientName) {
  selectedClientName = clientName;
  refresh();
}

async function disconnectClient(clientName) {
  if (!window.confirm(`Ngắt kết nối client "${clientName}"?`)) return;
  const token = window.prompt('Nhập MONITOR_ADMIN_TOKEN:');
  if (token === null) return;
  try {
    const response = await fetch(`/api/clients/${encodeURIComponent(clientName)}/disconnect`, {
      method: 'POST',
      headers: { 'X-Admin-Token': token }
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || `HTTP ${response.status}`);
    window.alert(result.message);
    refresh();
  } catch (error) {
    window.alert(`Không thể ngắt kết nối client: ${error.message}`);
  }
}

refresh();
setInterval(refresh, 2500);
</script>
</body>
</html>
"""


def start_services() -> None:
    """Start TCP monitoring, offline checker, and HTTP dashboard.

    This module contains server services only; it never creates the desktop GUI.
    """
    ensure_ports_available()
    log(f"Starting monitoring services: TCP={TCP_PORT}, HTTP={HTTP_PORT}")
    if not db_manager.connect():
        raise SystemExit("MySQL is required; server will not start without persistent database storage.")
    if not db_manager.mark_all_clients_offline():
        db_manager.close()
        raise SystemExit("Could not initialize client statuses in MySQL; server will not start.")
    if not ADMIN_TOKEN:
        log("WARNING: MONITOR_ADMIN_TOKEN is not configured; dashboard disconnect is disabled.")

    threading.Thread(target=mark_offline_clients, daemon=True).start()
    threading.Thread(target=tcp_server, daemon=True).start()

    log(f"HTTP dashboard listening on {HTTP_PORT}")
    app.run(
        host=HTTP_HOST,
        port=HTTP_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    start_services()
