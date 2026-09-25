from __future__ import annotations

import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.message_protocol import (
    CMD_GET_ENDPOINT,
    CMD_GET_PEERS,
    CMD_LOGOUT,
    CMD_REGISTER,
    CMD_SEARCH_FILES,
    CMD_UPDATE_FILES,
    RES_PEER_LIST,
    RES_SEARCH_RESULTS,
)
from server.client_handler import ClientHandler
from server.peer_info import PeerInfo

DEFAULT_PORT = 8888


class ServerMain:
    def __init__(self, root=None, parent=None) -> None:
        self.online_peers = {}
        self.peer_handlers = {}
        self.peer_history = {}
        self.server_socket = None
        self.is_running = False
        self.thread_pool = ThreadPoolExecutor(max_workers=20)
        self.lock = threading.Lock()

        self.root = root or tk.Tk()
        self.parent = parent or self.root
        if parent is None:
            self.root.title("P2P Central Directory & Indexing Server")
            self.root.geometry("900x600")
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.init_ui()

    def init_ui(self) -> None:
        top_frame = ttk.LabelFrame(self.parent, text="Cấu hình Máy chủ Trung tâm")
        top_frame.pack(fill="x", padx=8, pady=8)

        ttk.Label(top_frame, text="Cổng Server:").pack(side="left", padx=(10, 5), pady=8)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.port_entry = ttk.Entry(top_frame, textvariable=self.port_var, width=8)
        self.port_entry.pack(side="left", padx=5)

        self.start_stop_button = tk.Button(
            top_frame,
            text="Bắt đầu Server",
            command=self.toggle_server,
            font=("Arial", 10, "bold"),
        )
        self.start_stop_button.pack(side="left", padx=10)

        self.status_label = tk.Label(top_frame, text="Trạng thái: ĐÃ DỪNG", font=("Arial", 10, "bold"), fg="red")
        self.status_label.pack(side="left", padx=10)

        clear_history_button = tk.Button(
            top_frame,
            text="Xóa lịch sử ngắt kết nối",
            command=self.clear_disconnect_history,
        )
        clear_history_button.pack(side="right", padx=10)

        main_paned = ttk.PanedWindow(self.parent, orient=tk.VERTICAL)
        main_paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        peer_frame = ttk.LabelFrame(main_paned, text="Danh sách Peer (Online & Lịch sử ngắt kết nối)")
        main_paned.add(peer_frame, weight=1)

        columns = ("username", "ip", "port", "files", "connected", "disconnected")
        self.peer_table = ttk.Treeview(peer_frame, columns=columns, show="headings")
        self.peer_table.heading("username", text="Username")
        self.peer_table.heading("ip", text="Địa chỉ IP")
        self.peer_table.heading("port", text="Cổng P2P")
        self.peer_table.heading("files", text="Số File chia sẻ")
        self.peer_table.heading("connected", text="Thời gian kết nối")
        self.peer_table.heading("disconnected", text="Thời gian ngắt kết nối")
        self.peer_table.column("username", width=110)
        self.peer_table.column("ip", width=130)
        self.peer_table.column("port", width=70)
        self.peer_table.column("files", width=100)
        self.peer_table.column("connected", width=160)
        self.peer_table.column("disconnected", width=160)

        peer_scrollbar = ttk.Scrollbar(peer_frame, orient="vertical", command=self.peer_table.yview)
        self.peer_table.configure(yscrollcommand=peer_scrollbar.set)
        self.peer_table.pack(side="left", fill="both", expand=True)
        peer_scrollbar.pack(side="right", fill="y")

        log_frame = ttk.LabelFrame(main_paned, text="Nhật ký Hoạt động Máy chủ")
        main_paned.add(log_frame, weight=1)

        self.log_area = tk.Text(
            log_frame,
            wrap="none",
            font=("Consolas", 10),
            bg="#1e1e1e",
            fg="#dcdcdc",
            insertbackground="white",
        )
        log_scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_area.yview)
        self.log_area.configure(yscrollcommand=log_scrollbar.set)
        self.log_area.pack(side="left", fill="both", expand=True)
        log_scrollbar.pack(side="right", fill="y")
        self.log_area.configure(state="disabled")

    def toggle_server(self) -> None:
        if not self.is_running:
            try:
                port = int(self.port_var.get().strip())
                if port < 1 or port > 65535:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Lỗi", "Số cổng không hợp lệ")
                return

            try:
                self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.server_socket.bind(("0.0.0.0", port))
                self.server_socket.listen(50)
                self.is_running = True
                self.start_stop_button.config(text="Dừng Server")
                self.status_label.config(text=f"Trạng thái: ĐANG CHẠY (Port {port})", fg="green")
                self.port_entry.config(state="disabled")
                self.log(f"Máy chủ Directory Server đã bật và lắng nghe trên cổng {port}...")
                self.thread_pool.submit(self.accept_connections)
            except OSError as exc:
                self.log(f"Không thể khởi động Server tại cổng {port}: {exc}")
                messagebox.showerror("Lỗi bật Server", str(exc))
        else:
            self.stop_server()

    def accept_connections(self) -> None:
        while self.is_running and self.server_socket is not None:
            try:
                client_socket, client_address = self.server_socket.accept()
                ip = client_address[0]
                port = client_address[1]
                self.log(f"Kết nối mới đến từ IP: {ip}:{port}")
                handler = ClientHandler(client_socket, self)
                self.thread_pool.submit(handler.run)
            except OSError as exc:
                if self.is_running:
                    self.log(f"Lỗi chấp nhận kết nối: {exc}")

    def register_peer(self, username: str, ip: str, p2p_port: int, handler) -> bool:
        key = username.lower()
        with self.lock:
            if key in self.online_peers:
                return False
            info = PeerInfo(username, ip, p2p_port)
            self.online_peers[key] = info
            self.peer_handlers[key] = handler
            self.peer_history[key] = {
                "username": username,
                "ip": ip,
                "port": p2p_port,
                "connected_at": info.get_connected_at(),
                "disconnected_at": None,
            }
        self.log(f"Đã đăng ký Peer: {username} -> {ip}:{p2p_port}")
        self.update_peer_table()
        return True

    def unregister_peer(self, username: str) -> None:
        if username is None:
            return
        key = username.lower()
        with self.lock:
            self.online_peers.pop(key, None)
            self.peer_handlers.pop(key, None)
            if key in self.peer_history:
                self.peer_history[key]["disconnected_at"] = datetime.now()
        self.log(f"Peer đã ngắt kết nối: {username}")
        self.update_peer_table()

    def clear_disconnect_history(self) -> None:
        with self.lock:
            self.peer_history = {
                key: entry
                for key, entry in self.peer_history.items()
                if key in self.online_peers
            }
        self.update_peer_table()
        self.log("Đã xóa lịch sử các peer đã ngắt kết nối.")

    def get_peer(self, username: str):
        if username is None:
            return None
        return self.online_peers.get(username.lower())

    def search_files(self, query: str, requester_user: str):
        results = []
        q = query.lower() if query else ""
        with self.lock:
            peers = list(self.online_peers.values())
        for peer in peers:
            if peer.get_username().lower() == requester_user.lower():
                continue
            for file_descriptor in peer.get_shared_files():
                if hasattr(file_descriptor, "get_file_name"):
                    filename = file_descriptor.get_file_name()
                else:
                    filename = str(file_descriptor)
                if not q or q in filename.lower():
                    results.append(file_descriptor)
        return results

    def build_peer_list_response(self) -> str:
        response = RES_PEER_LIST
        entries = []
        with self.lock:
            peers = list(self.online_peers.values())
        for peer in peers:
            entries.append(f"{peer.get_username()};{peer.get_ip_address()};{peer.get_p2p_port()}")
        if entries:
            response += "|" + "#".join(entries)
        return response

    def peer_list_for_json(self):
        with self.lock:
            peers = list(self.online_peers.values())
        return [
            {
                "name": peer.get_username(),
                "host": peer.get_ip_address(),
                "port": peer.get_p2p_port(),
            }
            for peer in peers
        ]

    def broadcast_peer_list(self) -> None:
        message = self.build_peer_list_response()
        with self.lock:
            handlers = list(self.peer_handlers.values())
        for handler in handlers:
            try:
                handler.send_response(message)
            except Exception as exc:
                self.log(f"Lỗi gửi Peer List: {exc}")

    def update_peer_table(self) -> None:
        self.root.after(0, self._update_peer_table)

    def _update_peer_table(self) -> None:
        for item in self.peer_table.get_children():
            self.peer_table.delete(item)
        with self.lock:
            online_peers = dict(self.online_peers)
            history_entries = list(self.peer_history.values())

        def sort_key(entry):
            is_online = entry["disconnected_at"] is None
            return (0 if is_online else 1, -entry["connected_at"].timestamp())

        history_entries.sort(key=sort_key)

        for entry in history_entries:
            key = entry["username"].lower()
            online_peer = online_peers.get(key)
            files_count = len(online_peer.get_shared_files()) if online_peer else 0
            connected_str = entry["connected_at"].strftime("%Y-%m-%d %H:%M:%S")
            disconnected_str = (
                entry["disconnected_at"].strftime("%Y-%m-%d %H:%M:%S")
                if entry["disconnected_at"] is not None
                else "Đang online"
            )
            self.peer_table.insert(
                "",
                "end",
                values=(
                    entry["username"],
                    entry["ip"],
                    entry["port"],
                    files_count,
                    connected_str,
                    disconnected_str,
                ),
            )

    def log(self, message: str) -> None:
        def write_log():
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_area.configure(state="normal")
            self.log_area.insert("end", f"[{timestamp}] {message}\n")
            self.log_area.see("end")
            self.log_area.configure(state="disabled")

        self.root.after(0, write_log)

    def stop_server(self) -> None:
        self.is_running = False
        try:
            if self.server_socket is not None:
                self.server_socket.close()
        except OSError:
            pass
        self.server_socket = None

        with self.lock:
            now = datetime.now()
            for key in self.online_peers:
                if key in self.peer_history:
                    self.peer_history[key]["disconnected_at"] = now
            self.online_peers.clear()
            self.peer_handlers.clear()
        self.update_peer_table()

        self.start_stop_button.config(text="Bắt đầu Server")
        self.status_label.config(text="Trạng thái: ĐÃ DỪNG", fg="red")
        self.port_entry.config(state="normal")
        self.log("Máy chủ đã dừng.")

    def on_close(self) -> None:
        if self.is_running:
            self.stop_server()
        self.thread_pool.shutdown(wait=False)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    server = ServerMain()
    server.run()
