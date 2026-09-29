from __future__ import annotations

import logging
import json
import os
import platform
from typing import Any

try:
    import psutil
except ImportError:
    psutil = None


logger = logging.getLogger(__name__)
TOP_N = 50
MAX_PROCESS_SNAPSHOT_COUNT = 1000
MAX_PROCESS_SNAPSHOT_PAYLOAD_BYTES = 262144
PROCESS_FIELDS = frozenset(
    {"pid", "name", "username", "cpu_percent", "memory_percent", "status"}
)
PROTECTED_PROCESSES = {
    "windows": frozenset(
        {
            "csrss.exe",
            "lsass.exe",
            "services.exe",
            "smss.exe",
            "system",
            "wininit.exe",
            "winlogon.exe",
        }
    ),
    "linux": frozenset({"init", "kthreadd", "systemd"}),
    "darwin": frozenset({"kernel_task", "launchd"}),
}


def collect_process_list(limit: int = TOP_N) -> list[dict[str, Any]]:
    if not 1 <= limit <= TOP_N:
        raise ValueError(f"Process list limit must be between 1 and {TOP_N}.")
    return _collect_process_records()[:limit]


def collect_process_snapshot() -> list[dict[str, Any]]:
    processes = _collect_process_records(
        max_count=MAX_PROCESS_SNAPSHOT_COUNT,
        require_complete=True,
    )
    payload_size = len(json.dumps(processes, separators=(",", ":")).encode("utf-8"))
    if payload_size > MAX_PROCESS_SNAPSHOT_PAYLOAD_BYTES:
        raise RuntimeError("Process snapshot exceeds the supported payload size.")
    return processes


def _collect_process_records(
    *,
    max_count: int | None = None,
    require_complete: bool = False,
) -> list[dict[str, Any]]:
    if psutil is None:
        raise RuntimeError("psutil is unavailable; process monitoring is disabled.")

    processes: list[dict[str, Any]] = []
    incomplete = False
    try:
        process_iter = psutil.process_iter(
            attrs=["pid", "name", "username", "cpu_percent", "memory_percent", "status"],
            ad_value=None,
        )
        for process in process_iter:
            try:
                info = process.info
                pid = info.get("pid")
                name = info.get("name")
                if isinstance(pid, int) and not isinstance(pid, bool) and pid <= 0:
                    continue
                if (
                    not isinstance(pid, int)
                    or isinstance(pid, bool)
                    or not isinstance(name, str)
                    or not name
                ):
                    if (
                        require_complete
                        and not (
                            isinstance(pid, int)
                            and not isinstance(pid, bool)
                            and info.get("status")
                            == getattr(psutil, "STATUS_STOPPED", "stopped")
                            and not name
                        )
                    ):
                        incomplete = True
                    continue
                processes.append(
                    {
                        "pid": pid,
                        "name": name[:256],
                        "username": (
                            info.get("username")[:256]
                            if isinstance(info.get("username"), str)
                            else None
                        ),
                        "cpu_percent": _bounded_percentage(info.get("cpu_percent")),
                        "memory_percent": _bounded_percentage(info.get("memory_percent")),
                        "status": (
                            info.get("status")[:64]
                            if isinstance(info.get("status"), str)
                            else None
                        ),
                    }
                )
            except psutil.ZombieProcess:
                continue
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                logger.info("Skipping process whose details are not accessible.")
                incomplete = True
            except OSError as error:
                logger.warning(
                    "Skipping process after operating-system error (%s).",
                    type(error).__name__,
                )
                incomplete = True
    except psutil.Error as error:
        logger.error(
            "Process enumeration failed (%s).",
            type(error).__name__,
        )
        raise RuntimeError("Could not enumerate process information.") from error

    if require_complete and incomplete:
        raise RuntimeError("Process enumeration returned an incomplete snapshot.")
    if max_count is not None and len(processes) > max_count:
        raise RuntimeError("Process snapshot exceeds the supported process count.")

    processes.sort(
        key=lambda item: (
            item["memory_percent"] or 0.0,
            item["cpu_percent"] or 0.0,
            item["pid"],
        ),
        reverse=True,
    )
    return processes


def _bounded_percentage(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        percentage = float(value)
    except OverflowError:
        return None
    if not 0.0 <= percentage <= 100.0:
        return None
    return round(percentage, 2)


def terminate_process(pid: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "error",
        "code": "FAILED",
        "pid": pid if type(pid) is int and pid > 0 else None,
        "name": None,
        "message": "Could not terminate process.",
    }
    if type(pid) is not int or pid <= 0 or pid > 4_294_967_295:
        result.update(code="INVALID_PID", message="PID must be a positive integer.")
        return result
    if pid <= 1 or pid == os.getpid():
        result.update(
            code="PROTECTED_PROCESS",
            message="This process is protected by the client safety policy.",
        )
        return result
    if psutil is None:
        result.update(
            code="UNAVAILABLE",
            message="Process management is unavailable because psutil is missing.",
        )
        return result

    try:
        process = psutil.Process(pid)
        process_name = process.name()
        result["name"] = process_name[:256] if isinstance(process_name, str) else None
        platform_name = platform.system().lower()
        protected_names = PROTECTED_PROCESSES.get(platform_name, frozenset())
        if result["name"] and result["name"].casefold() in protected_names:
            result.update(
                code="PROTECTED_PROCESS",
                message="This process is protected by the client safety policy.",
            )
            return result
        if not process.is_running():
            result.update(
                code="PROCESS_NOT_FOUND",
                message="The process is no longer running.",
            )
            return result
        process.terminate()
        process.wait(timeout=5)
    except psutil.ZombieProcess:
        result.update(code="PROCESS_NOT_FOUND", message="The process is no longer running.")
        return result
    except psutil.NoSuchProcess:
        result.update(code="PROCESS_NOT_FOUND", message="The process no longer exists.")
        return result
    except psutil.AccessDenied:
        result.update(
            code="ACCESS_DENIED",
            message="The client does not have permission to terminate this process.",
        )
        return result
    except psutil.TimeoutExpired:
        result.update(
            code="TERMINATION_TIMEOUT",
            message="The process did not exit before the termination timeout.",
        )
        return result
    except OSError as error:
        logger.warning(
            "Process termination failed for PID %s (%s).",
            pid,
            type(error).__name__,
        )
        result.update(
            code="FAILED",
            message="The operating system could not terminate this process.",
        )
        return result

    result.update(
        status="ok",
        code="PROCESS_TERMINATED",
        message="Process terminated successfully.",
    )
    return result
