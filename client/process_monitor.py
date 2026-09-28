from __future__ import annotations

import logging
from typing import Any

try:
    import psutil
except ImportError:
    psutil = None


logger = logging.getLogger(__name__)
TOP_N = 20
PROCESS_FIELDS = frozenset(
    {"pid", "name", "username", "cpu_percent", "memory_percent", "status"}
)


def collect_process_list(limit: int = TOP_N) -> list[dict[str, Any]]:
    if not 1 <= limit <= TOP_N:
        raise ValueError(f"Process list limit must be between 1 and {TOP_N}.")
    if psutil is None:
        raise RuntimeError("psutil is unavailable; process monitoring is disabled.")

    processes: list[dict[str, Any]] = []
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
                if not isinstance(pid, int) or not isinstance(name, str) or not name:
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
            except OSError as error:
                logger.warning(
                    "Skipping process after operating-system error (%s).",
                    type(error).__name__,
                )
    except psutil.Error as error:
        logger.error(
            "Process enumeration failed (%s).",
            type(error).__name__,
        )
        raise RuntimeError("Could not enumerate process information.") from error

    processes.sort(
        key=lambda item: (
            item["memory_percent"] or 0.0,
            item["cpu_percent"] or 0.0,
            item["pid"],
        ),
        reverse=True,
    )
    return processes[:limit]


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
