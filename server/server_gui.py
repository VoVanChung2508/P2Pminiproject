from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Callable

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
DEFAULT_HTTP_PORT = int(os.environ.get("MONITOR_HTTP_PORT", "8081"))
DEFAULT_TCP_PORT = int(os.environ.get("MONITOR_TCP_PORT", "8888"))
PROCESS_REFRESH_INTERVAL_MS = 10_000


def is_port_available(port: int, host: str = "0.0.0.0") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return True
    except OSError:
        return False


def find_available_port(preferred: int, host: str = "0.0.0.0", max_attempts: int = 50) -> int:
    for p in range(preferred, preferred + max_attempts):
        if is_port_available(p, host):
            return p
    return preferred


class ServerManagerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Network Monitoring System - Server Manager")
        self.root.geometry("880x560")
        self.root.minsize(750, 480)

        self.process: subprocess.Popen[str] | None = None
        self.polling = True
        self.clients_by_name: dict[str, dict[str, object]] = {}
        self.selected_client_name: str | None = None

        initial_tcp = DEFAULT_TCP_PORT if is_port_available(DEFAULT_TCP_PORT) else find_available_port(DEFAULT_TCP_PORT)
        initial_http = DEFAULT_HTTP_PORT if is_port_available(DEFAULT_HTTP_PORT) else find_available_port(8081)

        self.tcp_port_var = tk.StringVar(value=str(initial_tcp))
        self.http_port_var = tk.StringVar(value=str(initial_http))
        self.status_var = tk.StringVar(value="STOPPED")
        self.summary_var = tk.StringVar(value="Máy chủ giám sát chưa khởi động")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(1000, self.refresh)

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=14)
        top.pack(fill="x")
        ttk.Label(
            top,
            text="NETWORK MONITORING SYSTEM",
            font=("Segoe UI", 16, "bold"),
            foreground="#0369a1",
        ).pack(side="left")
        self.status_badge = ttk.Label(
            top,
            textvariable=self.status_var,
            font=("Segoe UI", 11, "bold"),
            foreground="#dc2626",
        )
        self.status_badge.pack(side="right")

        # Port configuration & controls
        ctrl_frame = ttk.LabelFrame(self.root, text="Cấu hình & Điều khiển Server", padding=10)
        ctrl_frame.pack(fill="x", padx=14, pady=(0, 10))

        c_row = ttk.Frame(ctrl_frame)
        c_row.pack(fill="x")

        ttk.Label(c_row, text="TCP Port:").pack(side="left", padx=(0, 4))
        self.tcp_entry = ttk.Entry(c_row, textvariable=self.tcp_port_var, width=8)
        self.tcp_entry.pack(side="left", padx=(0, 12))

        ttk.Label(c_row, text="HTTP Port:").pack(side="left", padx=(0, 4))
        self.http_entry = ttk.Entry(c_row, textvariable=self.http_port_var, width=8)
        self.http_entry.pack(side="left", padx=(0, 16))

        self.start_button = ttk.Button(c_row, text="Start Server", command=self.start_server)
        self.start_button.pack(side="left", padx=(0, 8))

        self.stop_button = ttk.Button(c_row, text="Stop Server", command=self.stop_server, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))

        ttk.Button(c_row, text="Open Web Dashboard", command=self.open_dashboard).pack(side="left")

        # Admin status row
        admin_row = ttk.Frame(ctrl_frame)
        admin_row.pack(fill="x", pady=(6, 0))
        ttk.Label(admin_row, text="Admin Token:").pack(side="left", padx=(0, 4))
        self.admin_status_var = tk.StringVar(value="\u2014")
        self.admin_status_label = ttk.Label(
            admin_row,
            textvariable=self.admin_status_var,
            font=("Segoe UI", 10, "bold"),
        )
        self.admin_status_label.pack(side="left", padx=(0, 16))
        ttk.Label(admin_row, text="Process Control:").pack(side="left", padx=(0, 4))
        self.process_control_var = tk.StringVar(value="\u2014")
        self.process_control_label = ttk.Label(
            admin_row,
            textvariable=self.process_control_var,
            font=("Segoe UI", 10, "bold"),
        )
        self.process_control_label.pack(side="left")

        # Client table
        table_frame = ttk.Frame(self.root, padding=(14, 0))
        table_frame.pack(fill="both", expand=True)

        columns = ("client", "ip", "cpu", "ram", "disk", "network", "status")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = (
            ("client", "Client / Node", 140),
            ("ip", "Địa chỉ IP", 130),
            ("cpu", "CPU (%)", 85),
            ("ram", "RAM (%)", 85),
            ("disk", "Disk (%)", 85),
            ("network", "Network (%)", 95),
            ("status", "Trạng thái", 100),
        )
        for col, title, width in headings:
            self.table.heading(col, text=title)
            self.table.column(col, width=width, anchor="center")

        table_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=table_scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        table_scroll.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self._on_client_selection)
        self.process_button = ttk.Button(
            table_frame,
            text="Chi tiết / Tiến trình",
            command=self.open_client_processes,
            state="disabled",
        )
        self.process_button.pack(side="bottom", anchor="e", padx=8, pady=4)

        ttk.Label(self.root, textvariable=self.summary_var, padding=(14, 8)).pack(anchor="w")

        # Server log
        log_frame = ttk.LabelFrame(self.root, text="Nhật ký Máy chủ (Server Log)", padding=8)
        log_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log_box = tk.Text(log_frame, height=6, state="disabled", font=("Consolas", 9), bg="#0f172a", fg="#f1f5f9")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=log_scroll.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    def write_log(self, message: str) -> None:
        t = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{t}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def start_server(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return

        try:
            tcp_port = int(self.tcp_port_var.get().strip())
            http_port = int(self.http_port_var.get().strip())
        except ValueError:
            messagebox.showerror("Lỗi", "Số cổng TCP và HTTP phải là số nguyên hợp lệ.")
            return

        # Kiểm tra cổng trước khi khởi động server nền
        if not is_port_available(tcp_port):
            free_tcp = find_available_port(tcp_port + 1)
            if messagebox.askyesno(
                "Cổng TCP đang bị chiếm",
                f"Cổng TCP {tcp_port} hiện đang bị chiếm dụng bởi ứng dụng khác.\n\n"
                f"Bạn có muốn tự động đổi sang cổng khả dụng {free_tcp} không?",
            ):
                self.tcp_port_var.set(str(free_tcp))
                tcp_port = free_tcp
            else:
                return

        if not is_port_available(http_port):
            free_http = find_available_port(8081 if http_port == 8080 else http_port + 1)
            reason = " (bị chiếm bởi dịch vụ như Lenovo Vantage / AgentService)" if http_port == 8080 else ""
            if messagebox.askyesno(
                "Cổng HTTP đang bị chiếm",
                f"Cổng HTTP {http_port} hiện đang bị chiếm dụng{reason}.\n\n"
                f"Bạn có muốn tự động chuyển sang cổng khả dụng {free_http} không?",
            ):
                self.http_port_var.set(str(free_http))
                http_port = free_http
            else:
                return

        env = os.environ.copy()
        env["MONITOR_HTTP_PORT"] = str(http_port)
        env["MONITOR_TCP_PORT"] = str(tcp_port)

        self.process = subprocess.Popen(
            [sys.executable, "-m", "server.server"],
            cwd=str(PROJECT_ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        threading.Thread(target=self.read_process_log, daemon=True).start()
        self.write_log(f"Đang khởi động Server trên TCP: {tcp_port}, HTTP: {http_port}...")
        self.update_controls(True)

    def read_process_log(self) -> None:
        p = self.process
        if p is None or p.stdout is None:
            return
        for line in p.stdout:
            self.root.after(0, self.write_log, line.rstrip())
        code = p.wait()
        if code != 0:
            self.root.after(
                0,
                self.write_log,
                f"Tiến trình Server dừng với mã lỗi {code}. Xem các dòng lỗi phía trên để biết nguyên nhân.",
            )

    def stop_server(self) -> None:
        if self.process is None or self.process.poll() is not None:
            self.update_controls(False)
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.write_log("Máy chủ đã dừng.")
        self.update_controls(False)

    def update_controls(self, running: bool) -> None:
        self.status_var.set("RUNNING" if running else "STOPPED")
        self.status_badge.configure(foreground="#16a34a" if running else "#dc2626")
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.tcp_entry.configure(state="disabled" if running else "normal")
        self.http_entry.configure(state="disabled" if running else "normal")

    def open_dashboard(self) -> None:
        http_port = self.http_port_var.get().strip() or str(DEFAULT_HTTP_PORT)
        webbrowser.open(f"http://127.0.0.1:{http_port}")

    def refresh(self) -> None:
        if not self.polling:
            return
        running = self.process is not None and self.process.poll() is None
        if not running:
            self.update_controls(False)
        else:
            self.update_controls(True)
            threading.Thread(target=self.fetch_clients, daemon=True).start()
        self.root.after(2500, self.refresh)

    def fetch_clients(self) -> None:
        http_port = self.http_port_var.get().strip() or str(DEFAULT_HTTP_PORT)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/api/clients", timeout=1) as resp:
                data = json.loads(resp.read().decode("utf-8")).get("clients", [])
        except (urllib.error.URLError, TimeoutError, OSError):
            data = []
        admin_enabled = False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/api/health", timeout=1) as resp:
                health = json.loads(resp.read().decode("utf-8"))
                admin_enabled = bool(health.get("admin_disconnect_enabled", False))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            pass
        self.root.after(0, self.render_clients, data)
        self.root.after(0, self._update_admin_status, admin_enabled)

    def render_clients(self, clients: list[dict[str, object]]) -> None:
        selected_name = self.selected_client_name
        self.clients_by_name = {
            str(client.get("name", "")): client
            for client in clients
            if client.get("name")
        }
        for item in self.table.get_children():
            self.table.delete(item)
        online = 0
        for client in clients:
            status = str(client.get("status", "OFFLINE"))
            online += (status == "ONLINE")
            item_id = self.table.insert(
                "",
                "end",
                values=(
                    client.get("name", ""),
                    client.get("ip", ""),
                    f"{client.get('cpu', 0)}%",
                    f"{client.get('ram', 0)}%",
                    f"{client.get('disk', 0)}%",
                    f"{client.get('network', 0)}%",
                    status,
                ),
            )
            if str(client.get("name", "")) == selected_name:
                self.table.selection_set(item_id)
        if selected_name not in self.clients_by_name:
            self.selected_client_name = None
            self.process_button.configure(state="disabled")
        tcp_port = self.tcp_port_var.get().strip()
        http_port = self.http_port_var.get().strip()
        self.summary_var.set(

            f"Tổng thiết bị: {len(clients)} | Đang Online: {online} | TCP Port: {tcp_port} | HTTP Port: {http_port}"
        )

    def _update_admin_status(self, enabled: bool) -> None:
        if enabled:
            self.admin_status_var.set("CONFIGURED")
            self.admin_status_label.configure(foreground="#16a34a")
            self.process_control_var.set("ENABLED")
            self.process_control_label.configure(foreground="#16a34a")
        else:
            self.admin_status_var.set("NOT CONFIGURED")
            self.admin_status_label.configure(foreground="#dc2626")
            self.process_control_var.set("DISABLED")
            self.process_control_label.configure(foreground="#dc2626")

    def _on_client_selection(self, _event: tk.Event | None = None) -> None:
        selection = self.table.selection()
        if not selection:
            self.selected_client_name = None
            self.process_button.configure(state="disabled")
            return
        values = self.table.item(selection[0], "values")
        if not values:
            return
        self.selected_client_name = str(values[0])
        self.process_button.configure(state="normal")

    def open_client_processes(self) -> None:
        client = self.clients_by_name.get(self.selected_client_name or "")
        if client is None:
            messagebox.showerror("Lỗi", "Hãy chọn một máy khách trước.")
            return
        ClientProcessWindow(
            self.root,
            str(self.http_port_var.get().strip() or DEFAULT_HTTP_PORT),
            client,
        )

    def close(self) -> None:
        self.polling = False
        if self.process is not None and self.process.poll() is None:
            if messagebox.askyesno("Thoát", "Dừng máy chủ giám sát và đóng ứng dụng?"):
                self.stop_server()
            else:
                return
        self.root.destroy()


class ClientProcessWindow:
    def __init__(
        self,
        parent: tk.Misc,
        http_port: str,
        client: dict[str, object],
    ) -> None:
        self.window = tk.Toplevel(parent)
        self.client_name = str(client.get("name", ""))
        self.http_port = http_port
        self.processes: list[dict[str, object]] = []
        self.selected_process: dict[str, object] | None = None
        self.admin_token: str | None = None
        self.command_in_progress = False
        self.auto_refresh_enabled = False
        self.last_successful_update: float | None = None
        self.window.title(f"Tiến trình máy khách - {self.client_name}")
        self.window.geometry("940x560")
        self.window.minsize(760, 440)

        details = ttk.LabelFrame(self.window, text="Thông tin máy khách", padding=10)
        details.pack(fill="x", padx=12, pady=10)
        ttk.Label(
            details,
            text=(
                f"Client: {self.client_name}    "
                f"IP: {client.get('ip', '—')}    "
                f"Status: {client.get('status', 'OFFLINE')}    "
                f"CPU: {client.get('cpu', 0)}%    "
                f"RAM: {client.get('ram', 0)}%    "
                f"Disk: {client.get('disk', 0)}%"
            ),
        ).pack(anchor="w")

        controls = ttk.Frame(self.window, padding=(12, 0))
        controls.pack(fill="x")
        ttk.Label(controls, text="Tìm tiến trình:").pack(side="left")
        self.search_var = tk.StringVar()
        search = ttk.Entry(controls, textvariable=self.search_var, width=24)
        search.pack(side="left", padx=(6, 14))
        ttk.Label(controls, text="Sắp xếp:").pack(side="left")
        self.sort_var = tk.StringVar(value="RAM")
        sort = ttk.Combobox(
            controls,
            textvariable=self.sort_var,
            values=("RAM", "CPU", "PID"),
            state="readonly",
            width=10,
        )
        sort.pack(side="left", padx=(6, 14))
        ttk.Label(controls, text="Trạng thái:").pack(side="left")
        self.status_filter_var = tk.StringVar(value="Tất cả")
        status_filter = ttk.Combobox(
            controls,
            textvariable=self.status_filter_var,
            values=("Tất cả", "running", "sleeping", "stopped", "zombie"),
            state="readonly",
            width=12,
        )
        status_filter.pack(side="left", padx=(6, 10))
        ttk.Button(
            controls,
            text="Làm mới",
            command=self.refresh_processes,
        ).pack(side="left")

        table_frame = ttk.Frame(self.window, padding=12)
        table_frame.pack(fill="both", expand=True)
        columns = ("pid", "name", "username", "cpu", "ram", "status")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings")
        for column, heading, width in (
            ("pid", "PID", 90),
            ("name", "Tiến trình", 240),
            ("username", "Người dùng", 170),
            ("cpu", "CPU %", 90),
            ("ram", "RAM %", 90),
            ("status", "Trạng thái", 110),
        ):
            self.table.heading(column, text=heading)
            self.table.column(column, width=width, anchor="center")
        scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.table.yview,
        )
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.table.bind("<<TreeviewSelect>>", self._on_process_selection)
        self.search_var.trace_add("write", self._render_processes)
        sort.bind("<<ComboboxSelected>>", self._render_processes)
        status_filter.bind("<<ComboboxSelected>>", self._render_processes)

        self.message_var = tk.StringVar(value="Chưa tải danh sách tiến trình.")
        ttk.Label(self.window, textvariable=self.message_var, padding=(12, 0)).pack(
            anchor="w"
        )
        self.last_updated_var = tk.StringVar(value="Cập nhật gần nhất: chưa có")
        ttk.Label(
            self.window,
            textvariable=self.last_updated_var,
            padding=(12, 0),
        ).pack(anchor="w")
        actions = ttk.Frame(self.window, padding=12)
        actions.pack(fill="x")
        ttk.Button(actions, text="Đóng", command=self.window.destroy).pack(side="right")
        self.terminate_button = ttk.Button(
            actions,
            text="Kết thúc tiến trình...",
            command=self.terminate_selected,
            state="disabled",
        )
        self.terminate_button.pack(side="right", padx=(0, 8))

        self.window.after(100, self.refresh_processes)

    def _admin_token(self) -> str | None:
        if self.admin_token:
            return self.admin_token
        token = simpledialog.askstring(
            "Xác thực quản trị",
            "Nhập MONITOR_ADMIN_TOKEN:",
            show="*",
            parent=self.window,
        )
        if token is None:
            self.message_var.set("Đã hủy thao tác quản trị.")
            return None
        if not token:
            self.message_var.set("MONITOR_ADMIN_TOKEN không được để trống.")
            return None
        self.admin_token = token
        return token

    def refresh_processes(self) -> None:
        if self.command_in_progress:
            return
        token = self._admin_token()
        if token is None:
            return
        self.auto_refresh_enabled = True
        self._run_command(
            "GET_PROCESSES",
            None,
            token,
            self._show_process_list,
            schedule_refresh=True,
        )

    def terminate_selected(self) -> None:
        process = self.selected_process
        if process is None:
            return
        pid = process.get("pid")
        process_name = str(process.get("name", "unknown"))
        if type(pid) is not int:
            self.message_var.set("PID tiến trình không hợp lệ.")
            return
        if not messagebox.askyesno(
            "Xác nhận kết thúc tiến trình",
            f"Bạn có chắc muốn kết thúc tiến trình này?\n\n"
            f"Tiến trình: {process_name}\nPID: {pid}",
            parent=self.window,
        ):
            return
        token = self._admin_token()
        if token is None:
            return
        self._run_command(
            "TERMINATE_PROCESS",
            pid,
            token,
            lambda result: self._after_termination(
                result,
                token,
                pid,
                process_name,
            ),
        )

    def _run_command(
        self,
        command: str,
        pid: int | None,
        token: str,
        on_complete: Callable[[dict[str, object]], None],
        *,
        schedule_refresh: bool = False,
    ) -> None:
        if self.command_in_progress:
            return
        self.command_in_progress = True
        self.message_var.set(f"Đang gửi yêu cầu {command} đến {self.client_name}...")

        def worker() -> None:
            finished = False
            try:
                body: dict[str, object] = {"command": command}
                if pid is not None:
                    body["pid"] = pid
                queued = self._api_request(
                    "POST",
                    f"/api/clients/{urllib.parse.quote(self.client_name, safe='')}/commands",
                    token,
                    body,
                )
                cached_snapshot = queued.get("process_snapshot")
                if isinstance(cached_snapshot, dict):
                    self._dispatch(self._show_cached_snapshot, cached_snapshot)
                queued_request = queued.get("request")
                if not isinstance(queued_request, dict):
                    raise RuntimeError("Máy chủ không trả về trạng thái yêu cầu hợp lệ.")
                request_id = queued_request.get("request_id")
                if not isinstance(request_id, str):
                    raise RuntimeError("Máy chủ không trả về mã yêu cầu hợp lệ.")
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    time.sleep(0.4)
                    data = self._api_request(
                        "GET",
                        f"/api/clients/{urllib.parse.quote(self.client_name, safe='')}/commands",
                        token,
                    )
                    request_data = data.get("request")
                    if not isinstance(request_data, dict):
                        raise RuntimeError("Máy chủ trả về trạng thái yêu cầu không hợp lệ.")
                    if request_data.get("request_id") != request_id:
                        raise RuntimeError("Trạng thái lệnh không khớp yêu cầu hiện tại.")
                    status = request_data.get("status")
                    if status in {"complete", "error", "timeout"}:
                        request_data["process_snapshot_updated_at"] = (
                            data.get("process_snapshot_updated_at")
                            or request_data.get("process_snapshot_updated_at")
                        )
                        if isinstance(data.get("process_snapshot"), dict):
                            request_data["process_snapshot"] = data["process_snapshot"]
                        self._dispatch(self._finish_command, schedule_refresh)
                        finished = True
                        self._dispatch(on_complete, request_data)
                        return
                raise RuntimeError(
                    "Hết thời gian chờ phản hồi từ máy khách. "
                    "Hãy kiểm tra kết nối và thử làm mới."
                )
            except RuntimeError as error:
                if command == "GET_PROCESSES":
                    self._dispatch(self._show_refresh_error, str(error))
                else:
                    self._dispatch(self._show_error, str(error))
            finally:
                if not finished:
                    self._dispatch(self._finish_command, schedule_refresh)

        threading.Thread(target=worker, daemon=True).start()

    def _api_request(
        self,
        method: str,
        path: str,
        token: str,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        url = f"http://127.0.0.1:{self.http_port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "X-Admin-Token": token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 401:
                self.admin_token = None
            try:
                payload = json.loads(error.read().decode("utf-8"))
                message = (
                    payload.get("message", f"HTTP {error.code}")
                    if isinstance(payload, dict)
                    else f"HTTP {error.code}"
                )
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = f"HTTP {error.code}"
            raise RuntimeError(str(message)) from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Không thể liên lạc với API máy chủ ({type(error).__name__})."
            ) from error
        if not isinstance(result, dict):
            raise RuntimeError("Máy chủ trả về phản hồi JSON không hợp lệ.")
        if result.get("status") == "error":
            raise RuntimeError(str(result.get("message", "Yêu cầu thất bại.")))
        return result

    def _show_process_list(self, request_data: dict[str, object]) -> None:
        processes = request_data.get("result")
        if request_data.get("status") != "complete" or not isinstance(processes, list):
            snapshot = request_data.get("process_snapshot")
            if isinstance(snapshot, dict):
                self._show_cached_snapshot(snapshot)
                self._show_refresh_error(
                    self._request_error_message(
                        request_data,
                        "Đang hiển thị dữ liệu tiến trình gần nhất.",
                    )
                )
                return
            self._show_error(
                self._request_error_message(request_data, "Không thể tải tiến trình.")
            )
            return
        self.processes = [
            process
            for process in processes
            if isinstance(process, dict)
        ]
        updated_at = request_data.get("process_snapshot_updated_at")
        if isinstance(updated_at, (int, float)):
            self.last_successful_update = float(updated_at)
            self.last_updated_var.set(
                "Cập nhật gần nhất: "
                + datetime.fromtimestamp(self.last_successful_update).strftime("%H:%M:%S")
            )
        self._render_processes()
        self.message_var.set(
            f"Đã tải {len(self.processes)} tiến trình từ {self.client_name}."
        )

    def _show_cached_snapshot(self, snapshot: dict[str, object]) -> None:
        processes = snapshot.get("processes")
        if not isinstance(processes, list):
            return
        self.processes = [
            process for process in processes if isinstance(process, dict)
        ]
        updated_at = snapshot.get("updated_at")
        if isinstance(updated_at, (int, float)):
            self.last_successful_update = float(updated_at)
            self.last_updated_var.set(
                "Cập nhật gần nhất: "
                + datetime.fromtimestamp(self.last_successful_update).strftime("%H:%M:%S")
            )
        self._render_processes()

    def _show_refresh_error(self, message: str) -> None:
        self.message_var.set(
            f"Không thể cập nhật tiến trình ({message}); "
            "đang hiển thị dữ liệu gần nhất."
            if self.last_successful_update is not None
            else f"Thông tin tiến trình hiện không khả dụng ({message})."
        )

    def _finish_command(self, schedule_refresh: bool) -> None:
        self.command_in_progress = False
        if schedule_refresh:
            self._schedule_process_refresh()

    def _schedule_process_refresh(self) -> None:
        if not self.auto_refresh_enabled:
            return
        try:
            if self.window.winfo_exists():
                self.window.after(
                    PROCESS_REFRESH_INTERVAL_MS,
                    self.refresh_processes,
                )
        except tk.TclError:
            return

    def _after_termination(
        self,
        request_data: dict[str, object],
        token: str,
        pid: int,
        process_name: str,
    ) -> None:
        result = request_data.get("result")
        if (
            request_data.get("status") != "complete"
            or not isinstance(result, dict)
            or result.get("status") != "ok"
        ):
            self._show_error(
                self._request_error_message(
                    request_data,
                    "Yêu cầu kết thúc tiến trình thất bại.",
                )
            )
            return
        messagebox.showinfo(
            "Đã kết thúc tiến trình",
            f"Đã kết thúc {process_name} (PID {pid}) trên {self.client_name}.",
            parent=self.window,
        )
        self.message_var.set("Đang xác minh tiến trình đã dừng...")
        self._run_command(
            "GET_PROCESSES",
            None,
            token,
            lambda process_request: self._confirm_termination(
                process_request,
                pid,
                process_name,
            ),
        )

    def _confirm_termination(
        self,
        request_data: dict[str, object],
        pid: int,
        process_name: str,
    ) -> None:
        self._show_process_list(request_data)
        if request_data.get("status") != "complete":
            return
        if any(process.get("pid") == pid for process in self.processes):
            self._show_error(
                f"{process_name} (PID {pid}) vẫn xuất hiện trong danh sách mới."
            )
            return
        self.message_var.set(
            f"Đã xác nhận {process_name} (PID {pid}) không còn chạy."
        )

    @staticmethod
    def _request_error_message(
        request_data: dict[str, object],
        fallback: str,
    ) -> str:
        result = request_data.get("result")
        if isinstance(result, dict) and isinstance(result.get("message"), str):
            return result["message"]
        error_code = request_data.get("error_code")
        if error_code:
            return f"{error_code}: {fallback}"
        return fallback

    def _on_process_selection(self, _event: tk.Event | None = None) -> None:
        selection = self.table.selection()
        self.selected_process = None
        if selection:
            pid = self.table.item(selection[0], "values")[0]
            self.selected_process = next(
                (
                    process
                    for process in self.processes
                    if str(process.get("pid")) == str(pid)
                ),
                None,
            )
        self.terminate_button.configure(
            state="normal" if self.selected_process is not None else "disabled"
        )

    def _render_processes(self, *_args: object) -> None:
        if not hasattr(self, "table"):
            return
        selected_pid = (
            self.selected_process.get("pid")
            if self.selected_process is not None
            else None
        )
        self.selected_process = None
        self.terminate_button.configure(state="disabled")
        for item in self.table.get_children():
            self.table.delete(item)
        search = self.search_var.get().strip().casefold()
        selected_status = self.status_filter_var.get()
        sort_by = self.sort_var.get()
        fields = {
            "CPU": "cpu_percent",
            "RAM": "memory_percent",
            "PID": "pid",
        }
        field = fields.get(sort_by, "memory_percent")
        visible = [
            process
            for process in self.processes
            if search in str(process.get("name", "")).casefold()
            and (
                selected_status == "Tất cả"
                or process.get("status") == selected_status
            )
        ]
        visible.sort(
            key=lambda process: (
                float(process.get(field) or 0)
                if field != "pid"
                else int(process.get(field) or 0)
            ),
            reverse=field != "pid",
        )
        for process in visible:
            item = self.table.insert(
                "",
                "end",
                values=(
                    process.get("pid", ""),
                    process.get("name", ""),
                    process.get("username") or "—",
                    f"{float(process['cpu_percent']):.2f}"
                    if process.get("cpu_percent") is not None
                    else "—",
                    f"{float(process['memory_percent']):.2f}"
                    if process.get("memory_percent") is not None
                    else "—",
                    process.get("status") or "—",
                ),
            )
            if process.get("pid") == selected_pid:
                self.table.selection_set(item)
                self.selected_process = process
                self.terminate_button.configure(state="normal")

    def _show_error(self, message: str) -> None:
        self.message_var.set(message)
        messagebox.showerror("Lỗi thao tác tiến trình", message, parent=self.window)

    def _dispatch(
        self,
        callback: Callable[..., object],
        *args: object,
    ) -> None:
        try:
            self.window.after(0, callback, *args)
        except tk.TclError:
            return


if __name__ == "__main__":
    window = tk.Tk()
    ServerManagerGUI(window)
    window.mainloop()
