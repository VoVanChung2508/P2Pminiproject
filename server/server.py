from __future__ import annotations

import hmac
import json
import logging
import math
import socket
import threading
import time
import uuid
from os import environ
from typing import Any

from flask import Flask, jsonify, render_template_string, request
from common.command_auth import sign_process_command
from common.database import DatabaseManager
from common.logging_config import configure_logging

HTTP_HOST = "0.0.0.0"
HTTP_PORT = int(environ.get("MONITOR_HTTP_PORT", "8081"))
TCP_HOST = "0.0.0.0"
TCP_PORT = int(environ.get("MONITOR_TCP_PORT", "8888"))
HEARTBEAT_TIMEOUT = 15
TCP_CLIENT_TIMEOUT = 30
SAMPLE_LIMIT = 120
ADMIN_TOKEN = environ.get("MONITOR_ADMIN_TOKEN", "")
PROCESS_LIST_CAPABILITY = "PROCESS_LIST_V1"
CONTROLLED_COMMANDS_CAPABILITY = "CONTROLLED_COMMANDS_V1"
PROCESS_MANAGEMENT_CAPABILITY = "PROCESS_MANAGEMENT_V1"
CONTROLLED_COMMANDS = frozenset(
    {
        "PING",
        "GET_INFO",
        "GET_PROCESS_LIST",
        "GET_NETWORK_INFO",
        "GET_PROCESSES",
        "TERMINATE_PROCESS",
    }
)
PROCESS_MANAGEMENT_COMMANDS = frozenset({"GET_PROCESSES", "TERMINATE_PROCESS"})
MAX_REMOTE_PROCESS_PID = 4_294_967_295
PROCESS_LIST_TOP_N = 50
PROCESS_SNAPSHOT_MAX_COUNT = 1000
PROCESS_LIST_REQUEST_TIMEOUT_SECONDS = 30
PROCESS_LIST_MAX_PAYLOAD_BYTES = 262144
PROCESS_LIST_MAX_TCP_FRAME_BYTES = PROCESS_LIST_MAX_PAYLOAD_BYTES + 512
PROCESS_LIST_MAX_TRACKED_CLIENTS = 1000
PROCESS_LIST_RESULT_RETENTION_SECONDS = 300
CONTROLLED_COMMAND_REQUEST_TIMEOUT_SECONDS = 15
PROCESS_ACTIVITY_API_LIMIT = 50

app = Flask(__name__)
logger = logging.getLogger(__name__)
state_lock = threading.RLock()
clients: dict[str, dict[str, Any]] = {}
disconnected_clients: set[str] = set()
process_requests: dict[str, dict[str, Any]] = {}
controlled_command_requests: dict[str, dict[str, Any]] = {}
process_snapshots: dict[str, dict[str, Any]] = {}

db_manager = DatabaseManager()


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


def parse_network_rate(value: str, name: str) -> float | None:
    if value.strip().lower() == "null":
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return number


def parse_packet_counter(value: str, name: str) -> int | None:
    if value.strip().lower() == "null":
        return None
    if not value.isdigit():
        raise ValueError(f"{name} must be a non-negative integer")
    number = int(value)
    if number > 18446744073709551615:
        raise ValueError(f"{name} exceeds the supported counter range")
    return number


def register_client(
    name: str,
    ip: str,
    process_list_capable: bool = False,
    controlled_commands_capable: bool = False,
    process_management_capable: bool = False,
) -> None:
    key = name.lower()
    if not db_manager.register_client(name, ip):
        raise RuntimeError("MySQL did not save the client registration.")
    with state_lock:
        disconnected_clients.discard(key)
        controlled_command_requests.pop(key, None)
        process_snapshots.pop(key, None)
        clients[key] = {
            "name": name,
            "ip": ip,
            "status": "ONLINE",
            "last_seen_epoch": time.time(),
            "process_list_capable": process_list_capable,
            "controlled_commands_capable": controlled_commands_capable,
            "process_management_capable": process_management_capable,
        }
    logger.info("Client %s registered from %s.", name, ip)


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


def _mark_process_monitoring_unavailable_locked(client_key: str) -> None:
    snapshot = process_snapshots.get(client_key)
    if snapshot is None:
        client = clients.get(client_key, {})
        snapshot = {
            "client": client.get("name", client_key),
            "processes": None,
            "updated_at": None,
            "request_id": None,
        }
        process_snapshots[client_key] = snapshot
    snapshot["monitoring_status"] = "UNAVAILABLE"
    snapshot["failed_at"] = time.time()


def _diff_process_snapshots(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    previous_by_pid = {process["pid"]: process for process in previous}
    current_by_pid = {process["pid"]: process for process in current}
    events = []

    for pid in sorted(previous_by_pid.keys() | current_by_pid.keys()):
        old_process = previous_by_pid.get(pid)
        new_process = current_by_pid.get(pid)
        if old_process and (
            new_process is None or old_process["name"] != new_process["name"]
        ):
            events.append(
                {
                    "pid": pid,
                    "name": old_process["name"],
                    "event_type": "STOPPED",
                }
            )
        if new_process and (
            old_process is None or old_process["name"] != new_process["name"]
        ):
            events.append(
                {
                    "pid": pid,
                    "name": new_process["name"],
                    "event_type": "STARTED",
                }
            )
    return events


def _store_process_snapshot_locked(
    client_key: str,
    client_name: str,
    processes: list[dict[str, Any]],
    updated_at: float,
    request_id: str,
) -> bool:
    snapshot = process_snapshots.get(client_key)
    has_previous_snapshot = (
        snapshot is not None
        and isinstance(snapshot.get("processes"), list)
        and snapshot.get("updated_at") is not None
    )
    previous = (
        snapshot["processes"]
        if has_previous_snapshot and snapshot is not None
        else []
    )
    events = _diff_process_snapshots(previous, processes) if has_previous_snapshot else []
    if events and not db_manager.record_process_activity(client_name, events):
        _mark_process_monitoring_unavailable_locked(client_key)
        return False

    previous_by_pid = {process["pid"]: process for process in previous}
    current_processes = []
    for process in processes:
        current_process = dict(process)
        old_process = previous_by_pid.get(process["pid"])
        current_process["activity_status"] = (
            "RUNNING"
            if not has_previous_snapshot
            or (old_process is not None and old_process["name"] == process["name"])
            else "NEW"
        )
        current_processes.append(current_process)

    process_snapshots[client_key] = {
        "client": client_name,
        "processes": current_processes,
        "updated_at": updated_at,
        "last_successful_update": updated_at,
        "request_id": request_id,
        "monitoring_status": "AVAILABLE",
    }
    return True


def _expire_controlled_command_locked(client_key: str) -> None:
    command_request = controlled_command_requests.get(client_key)
    if (
        command_request
        and command_request["status"] in {"pending", "delivered"}
        and time.monotonic() >= command_request["expires_at"]
    ):
        command_request["status"] = "timeout"
        command_request["result"] = None
        command_request["completed_at"] = time.time()
        if command_request.get("command") == "GET_PROCESSES":
            _mark_process_monitoring_unavailable_locked(client_key)


def _prune_controlled_commands_locked(now: float) -> None:
    for client_key, command_request in tuple(controlled_command_requests.items()):
        if (
            command_request["status"] in {"pending", "delivered"}
            and now >= command_request["expires_at"]
        ):
            command_request["status"] = "timeout"
            command_request["result"] = None
            command_request["completed_at"] = time.time()
            if command_request.get("command") == "GET_PROCESSES":
                _mark_process_monitoring_unavailable_locked(client_key)
        elif (
            command_request["status"] not in {"pending", "delivered"}
            and now
            >= command_request["expires_at"] + PROCESS_LIST_RESULT_RETENTION_SECONDS
        ):
            del controlled_command_requests[client_key]


def _queue_controlled_command(
    name: str,
    command: str,
    *,
    pid: int | None = None,
    audit_id: int | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if command not in CONTROLLED_COMMANDS:
        return None, "Unsupported command."
    if command == "TERMINATE_PROCESS" and (
        type(pid) is not int or not 1 <= pid <= MAX_REMOTE_PROCESS_PID
    ):
        return None, "A valid positive process ID is required."
    if command != "TERMINATE_PROCESS" and pid is not None:
        return None, "A process ID is only valid for TERMINATE_PROCESS."

    key = name.lower()
    with state_lock:
        now = time.monotonic()
        _prune_controlled_commands_locked(now)
        client = clients.get(key)
        if (
            client is None
            or client["status"] != "ONLINE"
            or key in disconnected_clients
        ):
            return None, "Client is not registered or is offline."
        if not client.get("controlled_commands_capable", False):
            return None, "Client does not support controlled commands."
        if (
            command in PROCESS_MANAGEMENT_COMMANDS
            and not client.get("process_management_capable", False)
        ):
            return None, "Client does not support remote process management."
        if command in PROCESS_MANAGEMENT_COMMANDS and not ADMIN_TOKEN:
            return None, "Server admin token is not configured."
        _expire_controlled_command_locked(key)
        existing = controlled_command_requests.get(key)
        if existing and existing["status"] in {"pending", "delivered"}:
            return None, "A controlled command is already pending."
        existing_process_request = process_requests.get(key)
        if existing_process_request and existing_process_request["status"] in {
            "pending",
            "delivered",
        }:
            return None, "A process-list request is already pending."
        if (
            key not in controlled_command_requests
            and len(controlled_command_requests) >= PROCESS_LIST_MAX_TRACKED_CLIENTS
        ):
            return None, "Controlled command capacity is full."

        command_request = {
            "client": client["name"],
            "client_ip": client["ip"],
            "request_id": uuid.uuid4().hex,
            "command": command,
            "pid": pid,
            "audit_id": audit_id,
            "status": "pending",
            "requested_at": time.time(),
            "expires_at": now + CONTROLLED_COMMAND_REQUEST_TIMEOUT_SECONDS,
            "result": None,
        }
        controlled_command_requests[key] = command_request
        return dict(command_request), None


def _next_controlled_command(
    name: str,
    address: tuple[str, int],
) -> str | None:
    key = name.lower()
    with state_lock:
        client = clients.get(key)
        command_request = controlled_command_requests.get(key)
        if (
            client is None
            or not client.get("controlled_commands_capable", False)
            or command_request is None
            or command_request["status"] != "pending"
        ):
            return None
        _expire_controlled_command_locked(key)
        if command_request["status"] != "pending":
            return None
        command_request["status"] = "delivered"
        command_request["delivery_address"] = address
        command = command_request["command"]
        if command in PROCESS_MANAGEMENT_COMMANDS:
            argument = (
                str(command_request["pid"])
                if command == "TERMINATE_PROCESS"
                else ""
            )
            signature = sign_process_command(
                ADMIN_TOKEN,
                command_request["request_id"],
                client["name"],
                command,
                argument,
            )
            if command == "TERMINATE_PROCESS":
                return (
                    f"COMMAND|{command_request['request_id']}|{command}|"
                    f"{argument}|{signature}"
                )
            return (
                f"COMMAND|{command_request['request_id']}|{command}|"
                f"{signature}"
            )
        if command == "GET_PROCESS_LIST":
            return (
                f"COMMAND|{command_request['request_id']}|{command}|"
                f"{PROCESS_LIST_TOP_N}"
            )
        return f"COMMAND|{command_request['request_id']}|{command}"


def _validate_controlled_command_result(
    command: str,
    payload: str,
    expected_pid: int | None = None,
) -> Any:
    if len(payload.encode("utf-8")) > PROCESS_LIST_MAX_PAYLOAD_BYTES:
        raise ValueError("Command response exceeds the maximum size.")
    if command == "PING":
        if payload != "PONG":
            raise ValueError("PING response must be PONG.")
        return payload

    result = json.loads(payload)
    if command == "GET_PROCESS_LIST":
        return _validate_process_list(result, PROCESS_LIST_TOP_N)
    if command == "GET_PROCESSES":
        return _validate_process_list(result, PROCESS_SNAPSHOT_MAX_COUNT)
    if command == "TERMINATE_PROCESS":
        fields = {"status", "code", "pid", "name", "message"}
        if not isinstance(result, dict) or set(result) != fields:
            raise ValueError("Process termination result fields are invalid.")
        if (
            result["status"] not in {"ok", "error"}
            or result["code"]
            not in {
                "PROCESS_TERMINATED",
                "INVALID_PID",
                "PROCESS_NOT_FOUND",
                "ACCESS_DENIED",
                "PROTECTED_PROCESS",
                "TERMINATION_TIMEOUT",
                "UNAVAILABLE",
                "FAILED",
            }
            or isinstance(result["pid"], bool)
            or not isinstance(result["pid"], int)
            or result["pid"] <= 0
            or result["pid"] > MAX_REMOTE_PROCESS_PID
            or result["pid"] != expected_pid
            or (
                result["name"] is not None
                and (
                    not isinstance(result["name"], str)
                    or not result["name"]
                    or len(result["name"]) > 256
                )
            )
            or not isinstance(result["message"], str)
            or len(result["message"]) > 512
        ):
            raise ValueError("Process termination result is invalid.")
        if (
            result["status"] == "ok"
            and (
                result["code"] != "PROCESS_TERMINATED"
                or not result["name"]
            )
        ):
            raise ValueError("Process termination result does not match its request.")
        if (
            result["status"] == "error"
            and result["code"] == "PROCESS_TERMINATED"
        ):
            raise ValueError("Failed process termination has a success result.")
        return dict(result)
    if command == "GET_INFO":
        fields = {
            "client_name",
            "hostname",
            "os",
            "os_release",
            "machine",
            "python_version",
        }
        if (
            not isinstance(result, dict)
            or set(result) != fields
            or any(
                not isinstance(result[field], str)
                or not result[field]
                or len(result[field]) > 256
                for field in fields
            )
        ):
            raise ValueError("Client information response is invalid.")
        return dict(result)
    if command == "GET_NETWORK_INFO":
        fields = {
            "bytes_sent",
            "bytes_recv",
            "packets_sent",
            "packets_recv",
            "upload_bytes_per_sec",
            "download_bytes_per_sec",
        }
        if not isinstance(result, dict) or set(result) != fields:
            raise ValueError("Network information response fields are invalid.")
        for field in ("bytes_sent", "bytes_recv", "packets_sent", "packets_recv"):
            value = result[field]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"Network counter {field} is invalid.")
        for field in ("upload_bytes_per_sec", "download_bytes_per_sec"):
            value = result[field]
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f"Network rate {field} is invalid.")
        return dict(result)
    raise ValueError("Unsupported controlled command.")


def _accept_controlled_command_response(
    request_id: str,
    payload: str,
    address: tuple[str, int],
    *,
    error_code: str | None = None,
) -> str:
    if not request_id.isalnum() or len(request_id) > 64:
        return "ERROR|MALFORMED_COMMAND_RESPONSE"
    key = None
    with state_lock:
        for candidate, command_request in controlled_command_requests.items():
            if command_request["request_id"] == request_id:
                key = candidate
                break
        if key is None:
            return "ERROR|INVALID_COMMAND_REQUEST"
        command_request = controlled_command_requests[key]
        if (
            key not in clients
            or clients[key]["status"] != "ONLINE"
            or key in disconnected_clients
            or not clients[key].get("controlled_commands_capable", False)
            or command_request["status"] != "delivered"
            or command_request.get("delivery_address") != address
        ):
            return "ERROR|INVALID_COMMAND_REQUEST"
        _expire_controlled_command_locked(key)
        if command_request["status"] != "delivered":
            return "ERROR|COMMAND_TIMEOUT"
        command = command_request["command"]

    if error_code is not None:
        if error_code not in {
            "UNAVAILABLE",
            "FAILED",
            "UNSUPPORTED",
            "UNAUTHORIZED",
            "REPLAYED",
        }:
            return "ERROR|MALFORMED_COMMAND_RESPONSE"
        result = None
        status = "error"
    else:
        try:
            result = _validate_controlled_command_result(
                command,
                payload,
                command_request.get("pid"),
            )
        except (json.JSONDecodeError, RecursionError, ValueError, OverflowError) as error:
            if command == "GET_PROCESSES":
                with state_lock:
                    current = controlled_command_requests.get(key)
                    if (
                        current is not None
                        and current["request_id"] == request_id
                        and current["status"] == "delivered"
                    ):
                        current["status"] = "error"
                        current["result"] = None
                        current["error_code"] = "MALFORMED_RESULT"
                        current["completed_at"] = time.time()
                        _mark_process_monitoring_unavailable_locked(key)
            logger.warning(
                "Rejected malformed %s response from client %s (%s).",
                command,
                command_request["client"],
                type(error).__name__,
            )
            return "ERROR|MALFORMED_COMMAND_RESPONSE"
        status = "complete"

    audit_id = command_request.get("audit_id")
    audit_result = None
    audit_process_name = None
    audit_error = None
    if command == "TERMINATE_PROCESS" and isinstance(audit_id, int):
        if error_code is not None:
            audit_result = "FAILED"
            audit_error = error_code
        elif isinstance(result, dict):
            audit_process_name = result.get("name")
            if result.get("status") == "ok":
                audit_result = "SUCCESS"
            else:
                audit_result = "FAILED"
                audit_error = result.get("code", "FAILED")
        else:
            audit_result = "FAILED"
            audit_error = "MALFORMED_RESULT"
        audit_saved = db_manager.complete_process_termination_audit(
            audit_id,
            audit_process_name,
            audit_result,
            audit_error,
        )
        logger.info(
            "Remote process termination client=%s client_ip=%s action=TERMINATE_PROCESS "
            "pid=%s process=%s result=%s reason=%s audit_saved=%s",
            command_request["client"],
            command_request.get("client_ip"),
            command_request.get("pid"),
            audit_process_name or "unknown",
            audit_result,
            audit_error or "-",
            audit_saved,
        )

    with state_lock:
        current = controlled_command_requests.get(key)
        if (
            current is None
            or current["request_id"] != request_id
            or current["status"] != "delivered"
            or current.get("delivery_address") != address
        ):
            return "ERROR|INVALID_COMMAND_REQUEST"
        current["status"] = status
        current["result"] = result
        current["error_code"] = error_code
        current["completed_at"] = time.time()
        if command == "GET_PROCESSES" and status == "complete":
            if not _store_process_snapshot_locked(
                key,
                command_request["client"],
                result,
                current["completed_at"],
                request_id,
            ):
                current["status"] = "error"
                current["result"] = None
                current["error_code"] = "PROCESS_ACTIVITY_STORAGE_UNAVAILABLE"
                status = "error"
                error_code = current["error_code"]
        elif command == "GET_PROCESSES":
            _mark_process_monitoring_unavailable_locked(key)
        if command == "TERMINATE_PROCESS" and isinstance(audit_id, int):
            current["audit_status"] = "saved" if audit_saved else "error"
    logger.info(
        "Controlled command %s for client %s completed with status %s.",
        command,
        command_request["client"],
        status,
    )
    return (
        "ERROR|PROCESS_ACTIVITY_STORAGE"
        if command == "GET_PROCESSES"
        and error_code == "PROCESS_ACTIVITY_STORAGE_UNAVAILABLE"
        else "OK|COMMAND"
    )


def _known_process_name(client_key: str, pid: int) -> str | None:
    with state_lock:
        request_data = controlled_command_requests.get(client_key)
        if (
            request_data is None
            or request_data.get("command") != "GET_PROCESSES"
            or request_data.get("status") != "complete"
            or not isinstance(request_data.get("result"), list)
        ):
            return None
        for process in request_data["result"]:
            if isinstance(process, dict) and process.get("pid") == pid:
                return process.get("name")
    return None


def _start_termination_audit(
    name: str,
    pid: int | None,
    process_name: str | None,
) -> tuple[int | None, str, str | None]:
    key = name.lower()
    with state_lock:
        client = clients.get(key)
        client_name = str(client["name"]) if client else name[:100]
        client_ip = str(client["ip"]) if client else None
    logger.info(
        "Remote process termination client=%s client_ip=%s action=TERMINATE_PROCESS "
        "pid=%s process=%s result=REQUESTED",
        client_name,
        client_ip or "unknown",
        pid if pid is not None else "invalid",
        process_name or "unknown",
    )
    audit_id = db_manager.add_process_termination_audit(
        client_name,
        client_ip,
        pid,
        process_name,
    )
    return audit_id, client_name, client_ip


def _finish_termination_audit(
    audit_id: int | None,
    client_name: str,
    client_ip: str | None,
    pid: int | None,
    process_name: str | None,
    result: str,
    reason: str | None,
) -> bool:
    logger.info(
        "Remote process termination client=%s client_ip=%s action=TERMINATE_PROCESS "
        "pid=%s process=%s result=%s reason=%s",
        client_name,
        client_ip or "unknown",
        pid if pid is not None else "invalid",
        process_name or "unknown",
        result,
        reason or "-",
    )
    if audit_id is None:
        return False
    return db_manager.complete_process_termination_audit(
        audit_id,
        process_name,
        result,
        reason,
    )


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
        existing_command_request = controlled_command_requests.get(key)
        if existing_command_request and existing_command_request["status"] in {
            "pending",
            "delivered",
        }:
            return None, "A controlled command is already pending."
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
    if not isinstance(value, list) or len(value) > min(
        limit,
        PROCESS_SNAPSHOT_MAX_COUNT,
    ):
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
    logger.info("Client %s disconnected by server administrator.", client["name"])
    return True


def mark_offline_clients() -> None:
    while True:
        time.sleep(2)
        current_time = time.time()
        with state_lock:
            for client in clients.values():
                if current_time - client["last_seen_epoch"] > HEARTBEAT_TIMEOUT:
                    if client["status"] != "OFFLINE":
                        logger.warning(
                            "Client %s marked OFFLINE after heartbeat timeout.",
                            client["name"],
                        )
                        if not db_manager.update_status(client['name'], "OFFLINE"):
                            logger.error(
                                "Could not save OFFLINE status for client %s to MySQL.",
                                client["name"],
                            )
                    client["status"] = "OFFLINE"


def handle_message(message: str, address: tuple[str, int]) -> str:
    process_parts = message.strip().split("|", 3)
    process_command = process_parts[0].upper()
    if process_command in {"RESPONSE", "COMMAND_ERROR"}:
        response_parts = message.strip().split("|", 2)
        if len(response_parts) != 3:
            return "ERROR|MALFORMED_COMMAND_RESPONSE"
        if process_command == "RESPONSE":
            return _accept_controlled_command_response(
                response_parts[1],
                response_parts[2],
                address,
            )
        return _accept_controlled_command_response(
            response_parts[1],
            "",
            address,
            error_code=response_parts[2],
        )
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
                CONTROLLED_COMMANDS_CAPABILITY in parts[2:],
                PROCESS_MANAGEMENT_CAPABILITY in parts[2:],
            )
            return "OK|REGISTERED"
        if len(parts) >= 2 and is_client_disconnected(parts[1]):
            return "ERROR|DISCONNECTED"
        if command == "HEARTBEAT" and len(parts) >= 2:
            if not touch_client(parts[1]):
                reason = "DISCONNECTED" if is_client_disconnected(parts[1]) else "NOT_REGISTERED"
                logger.warning(
                    "Rejected heartbeat for client %s (%s).",
                    parts[1],
                    reason,
                )
                return f"ERROR|{reason}"
            controlled_command = _next_controlled_command(parts[1], address)
            if controlled_command is not None:
                logger.info(
                    "Delivered controlled command to client %s on heartbeat.",
                    parts[1],
                )
                return controlled_command
            process_command = _next_process_command(parts[1], address)
            if process_command is not None:
                logger.info(
                    "Delivered process-list request to client %s on heartbeat.",
                    parts[1],
                )
                return process_command
            logger.debug("Heartbeat received from client %s.", parts[1])
            return "OK|HEARTBEAT"
        if command == "SYSTEM" and len(parts) >= 2:
            metrics: dict[str, float | int | None] = {}
            for item in parts[2:]:
                key, value = item.split("=", 1)
                metric_name = key.lower()
                if metric_name in {"cpu", "ram", "disk", "network"}:
                    metrics[metric_name] = parse_metric(value.rstrip("%"), metric_name)
                elif metric_name in {
                    "upload_bps",
                    "download_bps",
                    "packets_sent",
                    "packets_recv",
                }:
                    api_name = {
                        "upload_bps": "upload_bytes_per_sec",
                        "download_bps": "download_bytes_per_sec",
                        "packets_sent": "packets_sent",
                        "packets_recv": "packets_recv",
                    }[metric_name]
                    if metric_name in {"upload_bps", "download_bps"}:
                        metrics[api_name] = parse_network_rate(value, api_name)
                    else:
                        metrics[api_name] = parse_packet_counter(value, api_name)
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
                logger.info("Client %s logged out.", client["name"])
            return "OK|LOGOUT"
        return "ERROR|Unsupported message"
    except (ValueError, IndexError) as exc:
        logger.warning(
            "Rejected malformed %s message from %s:%s (%s).",
            command if command in {"REGISTER", "SYSTEM", "HEARTBEAT", "LOGOUT"} else "unknown",
            address[0],
            address[1],
            type(exc).__name__,
        )
        return f"ERROR|{exc}"
    except RuntimeError as exc:
        logger.error(
            "Database operation failed while handling client message (%s).",
            type(exc).__name__,
        )
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
        logger.info("Monitoring TCP server listening on port %s.", TCP_PORT)
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
                    logger.warning(
                        "Port %s for %s is occupied (%s); switching to port %s.",
                        port,
                        service,
                        type(exc).__name__,
                        free_port,
                    )
                    globals()[port_var_name] = free_port
                    continue
                logger.error(
                    "Cannot start %s service on occupied port %s (%s).",
                    service,
                    port,
                    type(exc).__name__,
                )
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
        logger.error(
            "API client-list read failed (%s).",
            type(exc).__name__,
        )
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
        logger.error(
            "API history read failed for client %s (%s).",
            name,
            type(exc).__name__,
        )
        return jsonify({"status": "error", "message": str(exc)}), 503
    return jsonify({"client": name, "samples": samples})


@app.route("/api/alerts")
def api_alerts():
    try:
        stored_alerts = db_manager.get_alerts(100)
    except RuntimeError as exc:
        logger.error(
            "API alert read failed (%s).",
            type(exc).__name__,
        )
        return jsonify({"status": "error", "message": str(exc)}), 503
    return jsonify({"alerts": stored_alerts})


def _admin_token_error() -> tuple[str, int] | None:
    if not ADMIN_TOKEN:
        logger.warning("Rejected admin API request because no admin token is configured.")
        return "Server admin token is not configured", 503
    supplied_token = request.headers.get("X-Admin-Token", "")
    if not hmac.compare_digest(supplied_token, ADMIN_TOKEN):
        logger.warning("Rejected admin API request due to invalid credentials.")
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


@app.route("/api/clients/<name>/commands", methods=["GET", "POST"])
def api_controlled_commands(name: str):
    auth_error = _admin_token_error()
    if auth_error is not None:
        message, status_code = auth_error
        return jsonify({"status": "error", "message": message}), status_code

    key = name.lower()
    if request.method == "POST":
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("command"), str):
            logger.warning(
                "Rejected malformed controlled-command API request for client %s.",
                name,
            )
            return jsonify(
                {
                    "status": "error",
                    "message": 'Request body must be {"command": "<allowed command>"}',
                    "code": "MALFORMED_COMMAND",
                }
            ), 400
        command = body["command"]
        expected_fields = (
            {"command", "pid"}
            if command == "TERMINATE_PROCESS"
            else {"command"}
        )
        if set(body) != expected_fields:
            logger.warning(
                "Rejected malformed controlled-command API request for client %s.",
                name,
            )
            return jsonify(
                {
                    "status": "error",
                    "message": (
                        'TERMINATE_PROCESS requires exactly {"command": "...", "pid": integer}.'
                        if command == "TERMINATE_PROCESS"
                        else 'Request body must be {"command": "<allowed command>"}'
                    ),
                    "code": "MALFORMED_COMMAND",
                }
            ), 400
        if not isinstance(command, str) or command not in CONTROLLED_COMMANDS:
            logger.warning(
                "Rejected unsupported controlled-command API request for client %s.",
                name,
            )
            return jsonify(
                {
                    "status": "error",
                    "message": "Command is not in the supported allowlist.",
                    "code": "UNSUPPORTED_COMMAND",
                }
            ), 400

        pid = None
        audit_id = None
        audit_client_name = name[:100]
        audit_client_ip = None
        audit_process_name = None
        if command == "TERMINATE_PROCESS":
            raw_pid = body["pid"]
            if (
                type(raw_pid) is int
                and 1 <= raw_pid <= MAX_REMOTE_PROCESS_PID
            ):
                pid = raw_pid
                audit_process_name = _known_process_name(key, pid)
            audit_id, audit_client_name, audit_client_ip = (
                _start_termination_audit(
                    name,
                    pid,
                    audit_process_name,
                )
            )
            if audit_id is None:
                logger.error(
                    "Could not persist remote process termination audit request for client %s.",
                    audit_client_name,
                )
                return jsonify(
                    {
                        "status": "error",
                        "message": "Could not record the process termination audit request.",
                        "code": "AUDIT_UNAVAILABLE",
                    }
                ), 503
            if pid is None:
                audit_saved = _finish_termination_audit(
                    audit_id,
                    audit_client_name,
                    audit_client_ip,
                    None,
                    None,
                    "REJECTED",
                    "INVALID_PID",
                )
                if not audit_saved:
                    logger.error(
                        "Could not persist invalid process ID audit result for client %s.",
                        audit_client_name,
                    )
                return jsonify(
                    {
                        "status": "error",
                        "message": "PID must be a positive integer.",
                        "code": "INVALID_PID",
                    }
                ), 400

        command_request, error = _queue_controlled_command(
            name,
            command,
            pid=pid,
            audit_id=audit_id,
        )
        if error is not None:
            with state_lock:
                client = clients.get(key)
                if client is None:
                    error_code = "CLIENT_NOT_FOUND"
                elif client["status"] != "ONLINE" or key in disconnected_clients:
                    error_code = "CLIENT_OFFLINE"
                elif not client.get("controlled_commands_capable", False):
                    error_code = "COMMANDS_UNSUPPORTED"
                elif (
                    command in PROCESS_MANAGEMENT_COMMANDS
                    and not client.get("process_management_capable", False)
                ):
                    error_code = "PROCESS_MANAGEMENT_UNSUPPORTED"
                elif (
                    client.get("process_list_capable", False)
                    and not client.get("controlled_commands_capable", False)
                ):
                    error_code = "COMMANDS_UNSUPPORTED"
                elif (
                    (process_requests.get(key) or {}).get("status")
                    in {"pending", "delivered"}
                    or (controlled_command_requests.get(key) or {}).get("status")
                    in {"pending", "delivered"}
                ):
                    error_code = "COMMAND_PENDING"
                else:
                    error_code = "COMMAND_CAPACITY"
            status_code = (
                404
                if error_code == "CLIENT_NOT_FOUND"
                else 503
                if error_code == "COMMAND_CAPACITY"
                else 409
            )
            if command == "TERMINATE_PROCESS":
                audit_saved = _finish_termination_audit(
                    audit_id,
                    audit_client_name,
                    audit_client_ip,
                    pid,
                    audit_process_name,
                    "REJECTED",
                    error_code,
                )
                if not audit_saved:
                    logger.error(
                        "Could not persist rejected process termination audit result for client %s.",
                        audit_client_name,
                    )
            logger.warning(
                "Could not queue controlled command for client %s (%s).",
                name,
                error_code,
            )
            response = {
                "status": "error",
                "message": error,
                "code": error_code,
            }
            if command == "GET_PROCESSES":
                with state_lock:
                    snapshot = process_snapshots.get(key)
                    if snapshot is not None:
                        response["process_snapshot"] = dict(snapshot)
            return jsonify(response), status_code
        logger.info(
            "Queued controlled command %s for registered client %s.",
            command,
            name,
        )
        if command == "TERMINATE_PROCESS":
            logger.info(
                "Remote process termination client=%s client_ip=%s action=TERMINATE_PROCESS "
                "pid=%s process=%s result=QUEUED",
                audit_client_name,
                audit_client_ip or "unknown",
                pid,
                audit_process_name or "unknown",
            )
        command_request.pop("expires_at", None)
        command_request.pop("audit_id", None)
        command_request.pop("client_ip", None)
        response = {"status": "ok", "request": command_request}
        if command == "GET_PROCESSES":
            with state_lock:
                snapshot = process_snapshots.get(key)
                if snapshot is not None:
                    response["process_snapshot"] = dict(snapshot)
        return jsonify(response), 202

    with state_lock:
        _expire_controlled_command_locked(key)
        _prune_controlled_commands_locked(time.monotonic())
        command_request = controlled_command_requests.get(key)
        if command_request is None:
            snapshot = process_snapshots.get(key)
            if snapshot is not None:
                return jsonify(
                    {
                        "status": "ok",
                        "request": None,
                        "process_snapshot": dict(snapshot),
                    }
                )
            return jsonify(
                {"status": "error", "message": "No controlled command request found."}
            ), 404
        result = dict(command_request)
        result.pop("expires_at", None)
        audit_id = result.pop("audit_id", None)
        result.pop("client_ip", None)
        snapshot = process_snapshots.get(key)
        if snapshot is not None:
            result["process_snapshot_updated_at"] = snapshot.get("updated_at")
            result["process_snapshot"] = dict(snapshot)
        elif result.get("command") == "GET_PROCESSES":
            result["process_monitoring_status"] = "UNAVAILABLE"
    if (
        result.get("command") == "TERMINATE_PROCESS"
        and result.get("status") == "timeout"
        and isinstance(audit_id, int)
        and result.get("audit_status") != "saved"
    ):
        audit_saved = _finish_termination_audit(
            audit_id,
            str(result.get("client") or name),
            result.get("client_ip"),
            result.get("pid"),
            None,
            "TIMEOUT",
            "CLIENT_TIMEOUT_OR_DISCONNECTED",
        )
        with state_lock:
            current = controlled_command_requests.get(key)
            if current and current.get("request_id") == result.get("request_id"):
                current["audit_status"] = "saved" if audit_saved else "error"
                result["audit_status"] = current["audit_status"]
        if not audit_saved:
            logger.error(
                "Could not persist timed-out process termination audit result for client %s.",
                name,
            )
    return jsonify({"status": "ok", "request": result})


@app.get("/api/clients/<name>/process-activity")
def api_process_activity(name: str):
    auth_error = _admin_token_error()
    if auth_error is not None:
        message, status_code = auth_error
        return jsonify({"status": "error", "message": message}), status_code

    key = name.lower()
    with state_lock:
        if key not in clients:
            return jsonify({"status": "error", "message": "Client not found"}), 404
        snapshot = process_snapshots.get(key)
        snapshot = dict(snapshot) if snapshot is not None else None

    try:
        events = db_manager.get_process_activity(name, PROCESS_ACTIVITY_API_LIMIT)
    except RuntimeError as error:
        logger.error(
            "API process activity read failed for client %s (%s).",
            name,
            type(error).__name__,
        )
        return jsonify(
            {"status": "error", "message": "Could not read process activity history"}
        ), 503

    return jsonify(
        {
            "client": name,
            "monitoring_status": (
                snapshot.get("monitoring_status", "UNAVAILABLE")
                if snapshot is not None
                else "UNAVAILABLE"
            ),
            "last_successful_update": (
                snapshot.get("last_successful_update")
                if snapshot is not None
                else None
            ),
            "events": events,
        }
    )


@app.post("/api/clients/<name>/disconnect")
def api_disconnect_client(name: str):
    auth_error = _admin_token_error()
    if auth_error is not None:
        message, status_code = auth_error
        return jsonify({"status": "error", "message": message}), status_code
    try:
        disconnected = disconnect_client(name)
    except RuntimeError as error:
        logger.error(
            "API client disconnect failed for %s (%s).",
            name,
            type(error).__name__,
        )
        return jsonify(
            {"status": "error", "message": "Could not disconnect client"}
        ), 503
    if not disconnected:
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
.process-panel{margin-top:20px}
.process-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.process-controls input,.process-controls select{background:#0f172a;color:#e2e8f0;border:1px solid var(--border);border-radius:6px;padding:7px}
.process-controls button{background:#075985;color:#e0f2fe;border:1px solid #0369a1;border-radius:6px;padding:7px 10px;cursor:pointer}
.process-controls button:last-child{background:#7f1d1d;color:#fee2e2;border-color:#b91c1c}
.process-controls button:disabled{opacity:.45;cursor:not-allowed}
.process-message{min-height:20px;color:var(--subtext);font-size:13px}
.process-error{color:#fecaca;background:rgba(127,29,29,.45);border-left:3px solid var(--danger);padding:10px 12px;border-radius:0 6px 6px 0}
.process-row{cursor:pointer}
.process-row:hover{background:rgba(56,189,248,.05)}
.process-row.selected{background:rgba(56,189,248,.14)}
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
      <div class="chart-head"><h3>Network Traffic</h3><span class="chart-value" id="network-value">Up: — · Down: —</span></div>
      <div style="font-size:11px;color:var(--subtext);margin:-4px 0 8px">
        <span style="color:#a78bfa">● Upload</span>
        <span style="color:#34d399;margin-left:10px">● Download</span>
        <span> (bytes/sec)</span>
      </div>
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
              <th>Network (Up / Down)</th>
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

  <section class="panel process-panel" aria-labelledby="process-heading">
    <h2>
      <span id="process-heading">Tiến trình máy khách đã chọn</span>
      <span id="process-client-summary" style="font-size:12px;color:var(--subtext);font-weight:normal">
        Chọn một máy khách đang trực tuyến
      </span>
    </h2>
    <div class="process-controls">
      <label for="process-search">Tìm theo tên</label>
      <input id="process-search" type="search" autocomplete="off">
      <label for="process-status-filter">Trạng thái</label>
      <select id="process-status-filter">
        <option value="">Tất cả</option>
        <option value="running">running</option>
        <option value="sleeping">sleeping</option>
        <option value="stopped">stopped</option>
        <option value="zombie">zombie</option>
      </select>
      <label for="process-sort">Sắp xếp</label>
      <select id="process-sort">
        <option value="memory_percent">RAM</option>
        <option value="cpu_percent">CPU</option>
        <option value="pid">PID</option>
      </select>
      <button id="refresh-processes" type="button" disabled>Làm mới tiến trình</button>
      <button id="terminate-process" type="button" disabled>Kết thúc tiến trình...</button>
    </div>
    <p id="process-message" class="process-message" role="status">
      Chọn một client để yêu cầu danh sách tiến trình.
    </p>
    <p id="process-error" class="process-message process-error" role="alert" aria-live="assertive" hidden></p>
    <p id="process-last-update" class="process-message">
      Cập nhật gần nhất: chưa có
    </p>
    <p id="process-monitoring-status" class="process-message">
      Process Monitoring: UNAVAILABLE
    </p>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>PID</th>
            <th>Tiến trình</th>
            <th>Người dùng</th>
            <th>CPU %</th>
            <th>RAM %</th>
            <th>Trạng thái</th>
            <th>Hoạt động</th>
          </tr>
        </thead>
        <tbody id="processes">
          <tr><td colspan="7" class="empty">Chưa tải tiến trình</td></tr>
        </tbody>
      </table>
    </div>
    <h3>Hoạt động tiến trình gần đây</h3>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>Sự kiện</th>
            <th>PID</th>
            <th>Tiến trình</th>
            <th>Thời điểm</th>
          </tr>
        </thead>
        <tbody id="process-activity">
          <tr><td colspan="4" class="empty">Chưa có hoạt động tiến trình</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</div>

<script>
const chartSettings = {
  cpu: { color: '#38bdf8', valueEl: 'cpu-value', canvasId: 'cpu-chart' },
  ram: { color: '#22c55e', valueEl: 'ram-value', canvasId: 'ram-chart' },
  network: { color: '#a78bfa', valueEl: 'network-value', canvasId: 'network-chart' }
};
let selectedClientName = null;
let selectedProcessPid = null;
let clientSnapshots = [];
let processSnapshots = [];
let adminToken = null;
let processRequestGeneration = 0;
let processCommandInFlight = false;
let processLastUpdatedAt = null;
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

function formatRate(value) {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  let amount = Number(value);
  const units = ['B/s', 'KiB/s', 'MiB/s', 'GiB/s', 'TiB/s'];
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  return `${amount.toFixed(1)} ${units[unitIndex]}`;
}

function normalizeHistory(samples = []) {
  return samples.slice(-30).map((sample) => ({
    cpu: Number(sample.cpu || 0),
    ram: Number(sample.ram || 0),
    upload: sample.upload_bytes_per_sec == null ? null : Number(sample.upload_bytes_per_sec),
    download: sample.download_bytes_per_sec == null ? null : Number(sample.download_bytes_per_sec),
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
  const uploadValues = history.map(item => item.upload);
  const downloadValues = history.map(item => item.download);

  const cpuCurrent = cpuValues.length ? cpuValues[cpuValues.length - 1] : 0;
  const ramCurrent = ramValues.length ? ramValues[ramValues.length - 1] : 0;
  const uploadCurrent = uploadValues.length ? uploadValues[uploadValues.length - 1] : null;
  const downloadCurrent = downloadValues.length ? downloadValues[downloadValues.length - 1] : null;

  document.getElementById('cpu-value').textContent = `${cpuCurrent.toFixed(0)}%`;
  document.getElementById('ram-value').textContent = `${ramCurrent.toFixed(0)}%`;
  document.getElementById('network-value').textContent =
    `Up: ${formatRate(uploadCurrent)} · Down: ${formatRate(downloadCurrent)}`;

  drawChart('cpu-chart', cpuValues, '#38bdf8');
  drawChart('ram-chart', ramValues, '#22c55e');
  drawNetworkChart('network-chart', uploadValues, downloadValues);
}

function drawNetworkChart(canvasId, uploadValues, downloadValues) {
  const values = [...uploadValues, ...downloadValues]
    .filter(value => value != null && Number.isFinite(value));
  if (values.length === 0) {
    drawChart(canvasId, [], '#a78bfa');
    return;
  }

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

  const maxVal = Math.max(...values, 1);
  const stepX = (width - pad * 2) / Math.max(uploadValues.length - 1, 1);
  for (const [series, color] of [
    [uploadValues, '#a78bfa'],
    [downloadValues, '#34d399']
  ]) {
    ctx.beginPath();
    let started = false;
    series.forEach((value, index) => {
      if (value == null || !Number.isFinite(value)) {
        started = false;
        return;
      }
      const x = pad + index * stepX;
      const y = height - pad - ((value / maxVal) * (height - pad * 2));
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.5;
    ctx.stroke();
  }
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

    const previouslySelectedClient = selectedClientName;
    updateSelection(clientsData);
    clientSnapshots = clientsData;
    if (previouslySelectedClient !== selectedClientName) {
      processRequestGeneration += 1;
      processSnapshots = [];
      processLastUpdatedAt = null;
      selectedProcessPid = null;
      document.getElementById('process-activity').innerHTML =
        '<tr><td colspan="4" class="empty">Chưa có hoạt động tiến trình</td></tr>';
      document.getElementById('process-monitoring-status').textContent =
        'Process Monitoring: UNAVAILABLE';
      document.getElementById('process-last-update').textContent =
        'Cập nhật gần nhất: chưa có';
      document.getElementById('process-message').textContent =
        selectedClientName
          ? `Đã chọn ${selectedClientName}; tải tiến trình khi cần.`
          : 'Chọn một client để yêu cầu danh sách tiến trình.';
    }
    const processClient = selectedClient();
    if (!processClient || processClient.status !== 'ONLINE') {
      document.getElementById('process-message').textContent = processClient
        ? processLastUpdatedAt == null
          ? `Client ${processClient.name} đang ngoại tuyến; dữ liệu tiến trình hiện không khả dụng.`
          : `Client ${processClient.name} đang ngoại tuyến; đang hiển thị dữ liệu gần nhất.`
        : 'Chọn một client để yêu cầu danh sách tiến trình.';
      renderProcessRows();
    } else {
      renderProcessClient();
    }

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
          <td style="min-width:150px">↑ ${formatRate(c.upload_bytes_per_sec)}<br>↓ ${formatRate(c.download_bytes_per_sec)}</td>
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
  processRequestGeneration += 1;
  processSnapshots = [];
  processLastUpdatedAt = null;
  document.getElementById('process-activity').innerHTML =
    '<tr><td colspan="4" class="empty">Chưa có hoạt động tiến trình</td></tr>';
  document.getElementById('process-monitoring-status').textContent =
    'Process Monitoring: UNAVAILABLE';
  document.getElementById('process-last-update').textContent =
    'Cập nhật gần nhất: chưa có';
  selectedProcessPid = null;
  document.getElementById('process-message').textContent =
    `Đã chọn ${clientName}; đang tải tiến trình...`;
  renderProcessRows();
  refresh();
  refreshProcesses();
}

function requestAdminToken() {
  if (adminToken) return adminToken;
  const token = window.prompt('Nhập MONITOR_ADMIN_TOKEN:');
  if (token === null || token === '') return null;
  adminToken = token;
  return adminToken;
}

async function adminFetch(url, options = {}) {
  const token = requestAdminToken();
  if (!token) throw new Error('Cần MONITOR_ADMIN_TOKEN để thực hiện thao tác quản trị.');
  const response = await fetch(url, {
    ...options,
    headers: { ...(options.headers || {}), 'X-Admin-Token': token }
  });
  let result = {};
  try {
    result = await response.json();
  } catch (_error) {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  }
  if (!response.ok) {
    if (response.status === 401) adminToken = null;
    const error = new Error(result.message || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = result;
    throw error;
  }
  return result;
}

function formatProcessTrackingError(error) {
  if (error && error.status === 401) {
    return 'MONITOR_ADMIN_TOKEN không hợp lệ. Hãy nhập lại đúng token đang cấu hình trên server.';
  }
  if (
    error &&
    error.status === 503 &&
    error.message === 'Server admin token is not configured'
  ) {
    return 'Server chưa cấu hình MONITOR_ADMIN_TOKEN. Hãy cấu hình trên server rồi khởi động lại.';
  }
  if (error && error.message === 'UNAUTHORIZED') {
    return 'Máy khách từ chối chữ ký lệnh. Hãy kiểm tra MONITOR_ADMIN_TOKEN trên server và client giống nhau.';
  }
  if (
    error &&
    ['COMMAND_TIMEOUT', 'Hết thời gian chờ phản hồi từ máy khách.'].includes(error.message)
  ) {
    return 'Máy khách chưa phản hồi. Hãy kiểm tra máy khách còn trực tuyến, có gửi heartbeat và hỗ trợ theo dõi tiến trình.';
  }
  return error && error.message
    ? String(error.message)
    : 'Đã xảy ra lỗi không xác định khi theo dõi tiến trình.';
}

function showProcessError(error) {
  const alert = document.getElementById('process-error');
  alert.textContent = formatProcessTrackingError(error);
  alert.hidden = false;
}

function clearProcessError() {
  const alert = document.getElementById('process-error');
  alert.textContent = '';
  alert.hidden = true;
}

async function pollControlledCommand(clientName, requestId) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 400));
    const data = await adminFetch(
      `/api/clients/${encodeURIComponent(clientName)}/commands`
    );
    const request = data.request;
    if (!request || request.request_id !== requestId) {
      throw new Error('Trạng thái lệnh không khớp yêu cầu đang chờ.');
    }
    if (['complete', 'error', 'timeout'].includes(request.status)) {
      if (data.process_snapshot_updated_at != null) {
        request.process_snapshot_updated_at = data.process_snapshot_updated_at;
      }
      if (data.process_snapshot) request.process_snapshot = data.process_snapshot;
      return request;
    }
  }
  throw new Error('Hết thời gian chờ phản hồi từ máy khách.');
}

function selectedClient() {
  return clientSnapshots.find(client => client.name === selectedClientName) || null;
}

function renderProcessClient() {
  const client = selectedClient();
  const summary = document.getElementById('process-client-summary');
  const refreshButton = document.getElementById('refresh-processes');
  const terminateButton = document.getElementById('terminate-process');
  if (!client) {
    summary.textContent = 'Chọn một máy khách đang trực tuyến';
    refreshButton.disabled = true;
    terminateButton.disabled = true;
    return;
  }
  summary.textContent =
    `Client: ${client.name} · IP: ${client.ip} · ${client.status} · ` +
    `CPU ${client.cpu}% · RAM ${client.ram}% · Disk ${client.disk}%`;
  refreshButton.disabled = client.status !== 'ONLINE' || !adminDisconnectEnabled;
  terminateButton.disabled =
    client.status !== 'ONLINE' || !adminDisconnectEnabled || selectedProcessPid == null;
}

function renderProcessRows() {
  const tbody = document.getElementById('processes');
  const query = document.getElementById('process-search').value.trim().toLowerCase();
  const statusFilter = document.getElementById('process-status-filter').value;
  const sortBy = document.getElementById('process-sort').value;
  const rows = processSnapshots
    .filter(process =>
      String(process.name || '').toLowerCase().includes(query) &&
      (!statusFilter || process.status === statusFilter)
    )
    .sort((left, right) => {
      const leftValue = Number(left[sortBy]) || 0;
      const rightValue = Number(right[sortBy]) || 0;
      return sortBy === 'pid' ? leftValue - rightValue : rightValue - leftValue;
    });
  if (!rows.some(process => process.pid === selectedProcessPid)) {
    selectedProcessPid = null;
  }
  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">Không có tiến trình khớp bộ lọc</td></tr>';
    selectedProcessPid = null;
    renderProcessClient();
    return;
  }
  tbody.innerHTML = rows.map(process => `
    <tr class="process-row ${selectedProcessPid === process.pid ? 'selected' : ''}"
        data-process-pid="${Number(process.pid)}">
      <td>${Number(process.pid)}</td>
      <td>${escapeHtml(process.name)}</td>
      <td>${escapeHtml(process.username || '—')}</td>
      <td>${process.cpu_percent == null ? '—' : `${Number(process.cpu_percent).toFixed(2)}%`}</td>
      <td>${process.memory_percent == null ? '—' : `${Number(process.memory_percent).toFixed(2)}%`}</td>
      <td>${escapeHtml(process.status || '—')}</td>
      <td><strong>${escapeHtml(process.activity_status || 'RUNNING')}</strong></td>
    </tr>
  `).join('');
  tbody.querySelectorAll('tr[data-process-pid]').forEach(row => {
    row.addEventListener('click', () => {
      selectedProcessPid = Number(row.dataset.processPid);
      renderProcessRows();
    });
  });
  renderProcessClient();
}

function applyProcessSnapshot(snapshot) {
  if (!snapshot || !Array.isArray(snapshot.processes)) return false;
  processSnapshots = snapshot.processes;
  if (
    snapshot.updated_at != null &&
    Number.isFinite(Number(snapshot.updated_at))
  ) {
    processLastUpdatedAt = Number(snapshot.updated_at);
    document.getElementById('process-last-update').textContent =
      `Cập nhật gần nhất: ${new Date(processLastUpdatedAt * 1000).toLocaleTimeString()}`;
  }
  document.getElementById('process-monitoring-status').textContent =
    `Process Monitoring: ${snapshot.monitoring_status || 'AVAILABLE'}`;
  renderProcessRows();
  return true;
}

async function refreshProcessActivity(clientName, generation) {
  const status = document.getElementById('process-monitoring-status');
  const tbody = document.getElementById('process-activity');
  try {
    const data = await adminFetch(
      `/api/clients/${encodeURIComponent(clientName)}/process-activity`
    );
    if (
      generation !== processRequestGeneration ||
      selectedClientName !== clientName
    ) return;
    status.textContent = `Process Monitoring: ${data.monitoring_status}`;
    if (data.last_successful_update != null) {
      processLastUpdatedAt = Number(data.last_successful_update);
      document.getElementById('process-last-update').textContent =
        `Cập nhật gần nhất: ${new Date(processLastUpdatedAt * 1000).toLocaleTimeString()}`;
    } else {
      document.getElementById('process-last-update').textContent =
        'Cập nhật gần nhất: chưa có';
    }
    if (!Array.isArray(data.events) || data.events.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty">Chưa có hoạt động tiến trình</td></tr>';
      return;
    }
    tbody.innerHTML = data.events.map(event => `
      <tr>
        <td><strong>${event.event_type === 'STARTED' ? 'NEW' : 'STOPPED'}</strong></td>
        <td>${Number(event.pid)}</td>
        <td>${escapeHtml(event.process_name)}</td>
        <td>${escapeHtml(event.timestamp || '—')}</td>
      </tr>
    `).join('');
  } catch (error) {
    if (
      generation === processRequestGeneration &&
      selectedClientName === clientName
    ) {
      const errorMessage = formatProcessTrackingError(error);
      document.getElementById('process-monitoring-status').textContent =
        'Process Monitoring: UNAVAILABLE';
      showProcessError(error);
      tbody.innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(errorMessage)}</td></tr>`;
    }
  }
}

async function refreshProcesses() {
  const client = selectedClient();
  const message = document.getElementById('process-message');
  if (!client) {
    renderProcessClient();
    return;
  }
  if (!adminDisconnectEnabled) {
    message.textContent = 'Máy chủ chưa cấu hình MONITOR_ADMIN_TOKEN.';
    return;
  }
  if (client.status !== 'ONLINE') {
    message.textContent = `Client ${client.name} đang ngoại tuyến.`;
    return;
  }
  if (processCommandInFlight) return;
  clearProcessError();
  const generation = ++processRequestGeneration;
  processCommandInFlight = true;
  message.textContent = `Đang yêu cầu tiến trình từ ${client.name}...`;
  try {
    const queued = await adminFetch(
      `/api/clients/${encodeURIComponent(client.name)}/commands`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'GET_PROCESSES' })
      }
    );
    if (
      generation !== processRequestGeneration ||
      selectedClientName !== client.name
    ) return;
    applyProcessSnapshot(queued.process_snapshot);
    const requestId = queued.request && queued.request.request_id;
    if (typeof requestId !== 'string') throw new Error('Máy chủ không trả về mã yêu cầu hợp lệ.');
    const request = await pollControlledCommand(client.name, requestId);
    if (
      generation !== processRequestGeneration ||
      selectedClientName !== client.name
    ) return;
    if (request.status !== 'complete' || !Array.isArray(request.result)) {
      applyProcessSnapshot(request.process_snapshot);
      throw new Error(
        request.status === 'timeout'
          ? 'COMMAND_TIMEOUT'
          : request.error_code || 'Máy khách không thể trả danh sách tiến trình.'
      );
    }
    if (!applyProcessSnapshot(request.process_snapshot)) {
      processSnapshots = request.result;
      renderProcessRows();
    }
    await refreshProcessActivity(client.name, generation);
    message.textContent = `Đã nhận ${processSnapshots.length} tiến trình từ ${client.name}.`;
  } catch (error) {
    if (
      generation === processRequestGeneration &&
      selectedClientName === client.name
    ) {
      applyProcessSnapshot(error.payload && error.payload.process_snapshot);
      showProcessError(error);
      const errorMessage = formatProcessTrackingError(error);
      message.textContent = processLastUpdatedAt == null
        ? `Không thể tải tiến trình: ${errorMessage}`
        : `Không thể cập nhật tiến trình; đang hiển thị dữ liệu gần nhất (${new Date(processLastUpdatedAt * 1000).toLocaleTimeString()}): ${errorMessage}`;
    }
  } finally {
    processCommandInFlight = false;
  }
}

async function terminateSelectedProcess() {
  const client = selectedClient();
  const process = processSnapshots.find(item => item.pid === selectedProcessPid);
  if (!client || !process) return;
  if (!window.confirm(
    `Kết thúc tiến trình?\n\nTiến trình: ${process.name}\nPID: ${process.pid}`
  )) return;
  if (processCommandInFlight) return;
  processCommandInFlight = true;
  const generation = ++processRequestGeneration;
  const clientName = client.name;
  const message = document.getElementById('process-message');
  message.textContent = `Đang yêu cầu kết thúc ${process.name} trên ${client.name}...`;
  try {
    const queued = await adminFetch(
      `/api/clients/${encodeURIComponent(clientName)}/commands`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          command: 'TERMINATE_PROCESS',
          pid: Number(process.pid)
        })
      }
    );
    const requestId = queued.request && queued.request.request_id;
    if (typeof requestId !== 'string') throw new Error('Máy chủ không trả về mã yêu cầu hợp lệ.');
    const request = await pollControlledCommand(clientName, requestId);
    const result = request.result;
    if (request.status !== 'complete' || !result || result.status !== 'ok') {
      throw new Error(
        (result && result.message) ||
        request.error_code ||
        'Máy khách từ chối kết thúc tiến trình.'
      );
    }
    window.alert(`Đã kết thúc ${result.name} (PID ${result.pid}) trên ${clientName}.`);
    if (
      generation !== processRequestGeneration ||
      selectedClientName !== clientName
    ) return;
    message.textContent = 'Đang làm mới để xác nhận tiến trình đã dừng...';
    const refreshRequest = await queueAndPollProcessList(clientName);
    if (
      generation !== processRequestGeneration ||
      selectedClientName !== clientName
    ) return;
    if (refreshRequest.status !== 'complete' || !Array.isArray(refreshRequest.result)) {
      throw new Error('Đã kết thúc tiến trình nhưng không thể xác minh danh sách mới.');
    }
    processSnapshots = refreshRequest.result;
    if (
      refreshRequest.process_snapshot_updated_at != null &&
      Number.isFinite(Number(refreshRequest.process_snapshot_updated_at))
    ) {
      processLastUpdatedAt = Number(refreshRequest.process_snapshot_updated_at);
      document.getElementById('process-last-update').textContent =
        `Cập nhật gần nhất: ${new Date(processLastUpdatedAt * 1000).toLocaleTimeString()}`;
    }
    selectedProcessPid = null;
    renderProcessRows();
    const stillRunning = processSnapshots.some(item => item.pid === result.pid);
    message.textContent = stillRunning
      ? `${result.name} (PID ${result.pid}) vẫn xuất hiện trong danh sách mới.`
      : `Đã xác nhận ${result.name} (PID ${result.pid}) không còn chạy.`;
  } catch (error) {
    if (
      generation === processRequestGeneration &&
      selectedClientName === clientName
    ) {
      message.textContent = `Không thể kết thúc tiến trình: ${error.message}`;
    }
  } finally {
    processCommandInFlight = false;
  }
}

async function queueAndPollProcessList(clientName) {
  const queued = await adminFetch(
    `/api/clients/${encodeURIComponent(clientName)}/commands`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: 'GET_PROCESSES' })
    }
  );
  const requestId = queued.request && queued.request.request_id;
  if (typeof requestId !== 'string') throw new Error('Máy chủ không trả về mã yêu cầu hợp lệ.');
  return pollControlledCommand(clientName, requestId);
}

async function disconnectClient(clientName) {
  if (!window.confirm(`Ngắt kết nối client "${clientName}"?`)) return;
  try {
    const result = await adminFetch(
      `/api/clients/${encodeURIComponent(clientName)}/disconnect`,
      { method: 'POST' }
    );
    window.alert(result.message);
    refresh();
  } catch (error) {
    window.alert(`Không thể ngắt kết nối client: ${error.message}`);
  }
}

document.getElementById('process-search').addEventListener('input', renderProcessRows);
document.getElementById('process-status-filter').addEventListener('change', renderProcessRows);
document.getElementById('process-sort').addEventListener('change', renderProcessRows);
document.getElementById('refresh-processes').addEventListener('click', refreshProcesses);
document.getElementById('terminate-process').addEventListener('click', terminateSelectedProcess);
setInterval(() => {
  if (selectedClientName && adminToken) refreshProcesses();
}, 10000);

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
    configure_logging("server")
    ensure_ports_available()
    logger.info(
        "Starting monitoring services: TCP=%s, HTTP=%s.",
        TCP_PORT,
        HTTP_PORT,
    )
    if not db_manager.connect():
        logger.error(
            "MySQL is unavailable; server will not start without persistent storage."
        )
        raise SystemExit("MySQL is required; server will not start without persistent database storage.")
    if not db_manager.mark_all_clients_offline():
        db_manager.close()
        logger.error(
            "Could not initialize client statuses in MySQL; server will not start."
        )
        raise SystemExit("Could not initialize client statuses in MySQL; server will not start.")
    if not ADMIN_TOKEN:
        logger.warning(
            "MONITOR_ADMIN_TOKEN is not configured. Administrative operations are disabled."
        )
    else:
        logger.info("Admin authentication configured.")

    threading.Thread(target=mark_offline_clients, daemon=True).start()
    threading.Thread(target=tcp_server, daemon=True).start()

    logger.info("HTTP dashboard listening on port %s.", HTTP_PORT)
    app.run(
        host=HTTP_HOST,
        port=HTTP_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    start_services()
