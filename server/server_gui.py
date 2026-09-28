from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
DEFAULT_HTTP_PORT = int(os.environ.get("MONITOR_HTTP_PORT", "8081"))
DEFAULT_TCP_PORT = int(os.environ.get("MONITOR_TCP_PORT", "8888"))


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
        self.root.after(0, self.render_clients, data)

    def render_clients(self, clients: list[dict[str, object]]) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        online = 0
        for client in clients:
            status = str(client.get("status", "OFFLINE"))
            online += (status == "ONLINE")
            self.table.insert(
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
        tcp_port = self.tcp_port_var.get().strip()
        http_port = self.http_port_var.get().strip()
        self.summary_var.set(
            f"Tổng thiết bị: {len(clients)} | Đang Online: {online} | TCP Port: {tcp_port} | HTTP Port: {http_port}"
        )

    def close(self) -> None:
        self.polling = False
        if self.process is not None and self.process.poll() is None:
            if messagebox.askyesno("Thoát", "Dừng máy chủ giám sát và đóng ứng dụng?"):
                self.stop_server()
            else:
                return
        self.root.destroy()


if __name__ == "__main__":
    window = tk.Tk()
    ServerManagerGUI(window)
    window.mainloop()
