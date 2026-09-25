from __future__ import annotations

import os
import socket
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from common.file_descriptor import FileDescriptor
from common.message_protocol import (
    CMD_GET_PEERS,
    CMD_REGISTER,
    CMD_SEARCH_FILES,
    CMD_UPDATE_FILES,
    P2P_CMD_CHAT,
    RES_PEER_LIST,
    RES_SEARCH_RESULTS,
    build_message,
)
from peer.p2p_client_manager import P2PClientManager
from peer.p2p_server import P2PServer
from peer.shared_file_manager import SharedFileManager


class MainGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("P2P Hybrid Chat & File Transfer Client (Skype/Napster Model)")
        self.geometry("980x680")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.server_socket = None
        self.server_out = None
        self.server_in = None
        self.is_connected = False
        self.server_listen_thread = None

        self.my_username = ""
        self.my_p2p_port = 0

        user_home = Path.home()
        self.my_shared_folder = user_home / "P2P_Shared"
        self.my_shared_folder.mkdir(parents=True, exist_ok=True)
        self.my_downloads_folder = user_home / "P2P_Downloads"
        self.my_downloads_folder.mkdir(parents=True, exist_ok=True)

        self.shared_file_manager = SharedFileManager(self.my_shared_folder)
        self.p2p_server = None
        self.p2p_client_manager = P2PClientManager()
        self.online_peers = {}
        self.current_chat_peer = None

        self.init_ui()

    def init_ui(self) -> None:
        top_container = tk.Frame(self)
        top_container.pack(fill="x", padx=8, pady=8)

        conn_panel = tk.LabelFrame(top_container, text="Kết nối Máy chủ Trung tâm & Cấu hình P2P")
        conn_panel.pack(fill="x")

        tk.Label(conn_panel, text="Server Host:").grid(row=0, column=0, padx=5, pady=6, sticky="w")
        self.server_host_var = tk.StringVar(value="localhost")
        tk.Entry(conn_panel, textvariable=self.server_host_var, width=12).grid(row=0, column=1, padx=5, pady=6)

        tk.Label(conn_panel, text="Port:").grid(row=0, column=2, padx=5, pady=6, sticky="w")
        self.server_port_var = tk.StringVar(value="8888")
        tk.Entry(conn_panel, textvariable=self.server_port_var, width=7).grid(row=0, column=3, padx=5, pady=6)

        tk.Label(conn_panel, text="Username:").grid(row=0, column=4, padx=5, pady=6, sticky="w")
        self.username_var = tk.StringVar(value=f"User{os.urandom(2).hex()}")
        tk.Entry(conn_panel, textvariable=self.username_var, width=10).grid(row=0, column=5, padx=5, pady=6)

        tk.Label(conn_panel, text="Cổng P2P của tôi:").grid(row=0, column=6, padx=5, pady=6, sticky="w")
        self.p2p_port_var = tk.StringVar(value=str(9000 + (int.from_bytes(os.urandom(2), "big") % 900)))
        tk.Entry(conn_panel, textvariable=self.p2p_port_var, width=7).grid(row=0, column=7, padx=5, pady=6)

        self.connect_button = tk.Button(conn_panel, text="Kết nối Mạng P2P", command=self.toggle_connection, bg="#2980b9", fg="black")
        self.connect_button.grid(row=0, column=8, padx=8, pady=6)

        self.status_label = tk.Label(conn_panel, text="Trạng thái: OFFLINE", font=("Arial", 10, "bold"), fg="red")
        self.status_label.grid(row=0, column=9, padx=8, pady=6)

        folder_panel = tk.Frame(top_container)
        folder_panel.pack(fill="x", pady=(8, 0))
        tk.Button(folder_panel, text="Chọn Thư mục Chia sẻ...", command=self.choose_shared_folder, fg="black").pack(side="left")
        self.shared_folder_label = tk.Label(folder_panel, text=f"Thư mục Chia sẻ: {self.my_shared_folder}")
        self.shared_folder_label.pack(side="left", padx=10)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.chat_tab = tk.Frame(notebook)
        self.file_tab = tk.Frame(notebook)
        notebook.add(self.chat_tab, text="Direct P2P Chat (Skype Model)")
        notebook.add(self.file_tab, text="P2P File Sharing & Search (Napster Model)")

        self.setup_chat_tab()
        self.setup_file_tab()

    def setup_chat_tab(self) -> None:
        left = tk.Frame(self.chat_tab, width=220)
        left.pack(side="left", fill="y", padx=(8, 4), pady=8)
        left.pack_propagate(False)

        tk.Label(left, text="Danh sách Peer Online", font=("Arial", 10, "bold")).pack(anchor="w", padx=8, pady=(6, 4))
        self.peer_listbox = tk.Listbox(left, height=20)
        self.peer_listbox.pack(fill="both", expand=True, padx=8, pady=4)
        self.peer_listbox.bind("<<ListboxSelect>>", self.on_peer_selected)

        right = tk.Frame(self.chat_tab)
        right.pack(side="left", fill="both", expand=True, padx=(4, 8), pady=8)

        self.selected_chat_peer_label = tk.Label(right, text="Chọn một Peer đang online để bắt đầu Chat P2P")
        self.selected_chat_peer_label.pack(anchor="w", fill="x", pady=(4, 8))

        self.chat_transcript = tk.Text(right, wrap="word", state="disabled", height=20)
        self.chat_transcript.pack(fill="both", expand=True)

        input_frame = tk.Frame(right)
        input_frame.pack(fill="x", pady=(8, 0))
        self.chat_input = tk.Entry(input_frame)
        self.chat_input.pack(side="left", fill="x", expand=True)
        self.chat_input.bind("<Return>", lambda event: self.send_direct_message())
        tk.Button(input_frame, text="Gửi Tin Nhắn", command=self.send_direct_message, bg="#27ae60", fg="black").pack(side="left", padx=(8, 0))

    def setup_file_tab(self) -> None:
        search_bar = tk.Frame(self.file_tab)
        search_bar.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(search_bar, text="Từ khóa:").pack(side="left")
        self.search_var = tk.StringVar()
        tk.Entry(search_bar, textvariable=self.search_var, width=25).pack(side="left", padx=(8, 6))
        tk.Button(search_bar, text="Tìm File", command=self.perform_search, bg="#8e44ad", fg="black").pack(side="left")
        tk.Button(search_bar, text="Quét lại File Chia sẻ", command=self.refresh_and_publish_shared_files).pack(side="left", padx=(8, 0))

        table_frame = tk.Frame(self.file_tab)
        table_frame.pack(fill="both", expand=True, padx=8, pady=4)

        columns = ("file_name", "size", "owner", "endpoint")
        self.search_tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        self.search_tree.heading("file_name", text="Tên File")
        self.search_tree.heading("size", text="Kích thước")
        self.search_tree.heading("owner", text="Peer Sở hữu")
        self.search_tree.heading("endpoint", text="Endpoint")
        self.search_tree.column("file_name", width=220)
        self.search_tree.column("size", width=120)
        self.search_tree.column("owner", width=150)
        self.search_tree.column("endpoint", width=170)
        self.search_tree.pack(side="left", fill="both", expand=True)

        for col in columns:
            self.search_tree.heading(col, command=lambda _col=col: None)

        local_files_frame = tk.LabelFrame(table_frame, text="File Chia sẻ của Tôi")
        local_files_frame.pack(side="left", fill="y", padx=(8, 0))
        self.local_files_list = tk.Listbox(local_files_frame, width=32, height=20)
        self.local_files_list.pack(fill="both", expand=True, padx=6, pady=6)

        self.downloads_frame = tk.LabelFrame(self.file_tab, text="Tiến trình Tải File P2P")
        self.downloads_frame.pack(fill="x", padx=8, pady=(4, 8))
        self.downloads_tree = ttk.Treeview(self.downloads_frame, columns=("name", "size", "progress", "speed", "status"), show="headings")
        self.downloads_tree.heading("name", text="Tên File")
        self.downloads_tree.heading("size", text="Kích thước")
        self.downloads_tree.heading("progress", text="Tiến trình (%)")
        self.downloads_tree.heading("speed", text="Tốc độ (KB/s)")
        self.downloads_tree.heading("status", text="Trạng thái")
        self.downloads_tree.pack(fill="x")

    def on_peer_selected(self, event) -> None:
        selected = self.peer_listbox.curselection()
        if not selected:
            return
        peer_name = self.peer_listbox.get(selected[0])
        self.current_chat_peer = peer_name
        endpoint = self.online_peers.get(peer_name, "unknown")
        self.selected_chat_peer_label.config(text=f"Phiên Chat P2P Trực tiếp với: {peer_name} ({endpoint})")

    def append_chat_log(self, sender: str, message: str) -> None:
        self.chat_transcript.configure(state="normal")
        self.chat_transcript.insert("end", f"{sender}: {message}\n")
        self.chat_transcript.see("end")
        self.chat_transcript.configure(state="disabled")

    def toggle_connection(self) -> None:
        if not self.is_connected:
            host = self.server_host_var.get().strip()
            port_text = self.server_port_var.get().strip()
            username = self.username_var.get().strip()
            try:
                server_port = int(port_text)
                my_p2p_port = int(self.p2p_port_var.get().strip())
            except ValueError:
                messagebox.showerror("Lỗi", "Cổng không hợp lệ.")
                return

            if not username:
                messagebox.showerror("Lỗi", "Username không được trống.")
                return

            try:
                self.my_username = username
                self.my_p2p_port = my_p2p_port
                self.p2p_server = P2PServer(my_p2p_port, self.shared_file_manager, self)
                self.p2p_server.start()

                self.server_socket = socket.create_connection((host, server_port), timeout=10)
                self.server_out = self.server_socket.makefile("w", encoding="utf-8", newline="")
                self.server_in = self.server_socket.makefile("r", encoding="utf-8", newline="")
                self.server_out.write(build_message(CMD_REGISTER, self.my_username, str(self.my_p2p_port)) + "\n")
                self.server_out.flush()

                response = self.server_in.readline()
                if response and response.strip().startswith("REGISTER_OK"):
                    self.is_connected = True
                    self.connect_button.config(text="Ngắt kết nối", bg="#c0392b")
                    self.status_label.config(text=f"Trạng thái: ONLINE ({self.my_username})", fg="#27ae60")
                    self.server_listen_thread = threading.Thread(target=self.listen_to_server, daemon=True)
                    self.server_listen_thread.start()
                    self.refresh_and_publish_shared_files()
                    self.append_chat_log("Hệ thống", f"Đã kết nối thành công đến Máy chủ Trung tâm tại {host}:{server_port}")
                else:
                    err = response.strip() if response else "Đăng ký thất bại."
                    messagebox.showerror("Lỗi đăng ký", err)
                    self.p2p_server.stop()
                    self.server_socket.close()
            except Exception as exc:
                messagebox.showerror("Lỗi kết nối", f"Lỗi kết nối: {exc}")
                if self.p2p_server is not None:
                    self.p2p_server.stop()
        else:
            self.disconnect()

    def listen_to_server(self) -> None:
        try:
            while self.is_connected and self.server_in is not None:
                line = self.server_in.readline()
                if not line:
                    break
                self.process_server_message(line.strip())
        except Exception:
            if self.is_connected:
                self.append_chat_log("Hệ thống", "Mất kết nối đến Máy chủ Trung tâm.")
        finally:
            if self.is_connected:
                self.after(0, self.disconnect)

    def process_server_message(self, message: str) -> None:
        if not message:
            return
        tokens = message.split("|")
        command = tokens[0]
        if command == RES_PEER_LIST:
            self.handle_peer_list_response(tokens)
        elif command == RES_SEARCH_RESULTS:
            self.handle_search_results_response(tokens)

    def handle_peer_list_response(self, tokens) -> None:
        self.peer_listbox.delete(0, tk.END)
        self.online_peers.clear()
        if len(tokens) >= 2 and tokens[1]:
            for entry in tokens[1].split("#"):
                parts = entry.split(";")
                if len(parts) >= 3:
                    user = parts[0]
                    ip = parts[1]
                    port = parts[2]
                    if user.lower() != self.my_username.lower():
                        self.online_peers[user] = f"{ip}:{port}"
                        self.peer_listbox.insert(tk.END, user)

    def handle_search_results_response(self, tokens) -> None:
        for item in self.search_tree.get_children():
            self.search_tree.delete(item)
        if len(tokens) >= 2 and tokens[1]:
            for entry in tokens[1].split("#"):
                descriptor = FileDescriptor.from_protocol_string(entry)
                if descriptor is None:
                    continue
                endpoint = f"{descriptor.get_owner_ip()}:{descriptor.get_owner_p2p_port()}"
                self.search_tree.insert(
                    "",
                    "end",
                    values=(
                        descriptor.get_file_name(),
                        descriptor.format_file_size(descriptor.get_file_size()),
                        descriptor.get_owner_username(),
                        endpoint,
                    ),
                )

    def refresh_and_publish_shared_files(self) -> None:
        if not self.is_connected:
            return
        files = self.shared_file_manager.scan_shared_files(self.my_username, self.get_local_ip(), self.my_p2p_port)
        payload = "#".join(item.to_protocol_string() for item in files)
        self.server_out.write(build_message(CMD_UPDATE_FILES, payload) + "\n")
        self.server_out.flush()
        self.local_files_list.delete(0, tk.END)
        for file_path in sorted(self.my_shared_folder.iterdir(), key=lambda p: p.name.lower()):
            if file_path.is_file() and not file_path.name.startswith("."):
                self.local_files_list.insert(tk.END, file_path.name)

    def get_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def perform_search(self) -> None:
        if not self.is_connected:
            messagebox.showwarning("Thông báo", "Bạn cần kết nối trước khi tìm file.")
            return
        query = self.search_var.get().strip()
        self.server_out.write(build_message(CMD_SEARCH_FILES, query) + "\n")
        self.server_out.flush()

    def send_direct_message(self) -> None:
        if self.current_chat_peer is None:
            messagebox.showwarning("Thông báo", "Vui lòng chọn một peer để chat.")
            return
        message = self.chat_input.get().strip()
        if not message:
            return
        target_ip = self.online_peers.get(self.current_chat_peer, "").split(":")[0] if self.current_chat_peer in self.online_peers else ""
        target_port = self.online_peers.get(self.current_chat_peer, "").split(":")[1] if self.current_chat_peer in self.online_peers else ""
        if not target_ip or not target_port:
            messagebox.showwarning("Thông báo", "Peer đã offline hoặc thông tin endpoint không hợp lệ.")
            return

        self.p2p_client_manager.send_direct_chat_message_async(
            target_ip,
            int(target_port),
            self.my_username,
            message,
            callback=type("ChatCallback", (), {
                "on_success": lambda self_: self.append_chat_log("Bạn", message),
                "on_failure": lambda self_, err: self.append_chat_log("Hệ thống", f"Gửi tin nhắn thất bại: {err}"),
            })(),
        )
        self.chat_input.delete(0, tk.END)

    def choose_shared_folder(self) -> None:
        path = filedialog.askdirectory(title="Chọn Thư mục Chia sẻ")
        if not path:
            return
        self.my_shared_folder = Path(path)
        self.shared_file_manager.set_shared_directory(self.my_shared_folder)
        self.shared_folder_label.config(text=f"Thư mục Chia sẻ: {self.my_shared_folder}")
        self.refresh_and_publish_shared_files()

    def disconnect(self) -> None:
        self.is_connected = False
        if self.server_out is not None:
            try:
                self.server_out.write(build_message("LOGOUT") + "\n")
                self.server_out.flush()
            except Exception:
                pass
        if self.server_socket is not None:
            try:
                self.server_socket.close()
            except Exception:
                pass
        self.server_socket = None
        self.server_out = None
        self.server_in = None
        if self.p2p_server is not None:
            self.p2p_server.stop()
            self.p2p_server = None
        self.connect_button.config(text="Kết nối Mạng P2P", bg="#2980b9")
        self.status_label.config(text="Trạng thái: OFFLINE", fg="red")
        self.append_chat_log("Hệ thống", "Đã ngắt kết nối khỏi máy chủ trung tâm.")

    def on_direct_message_received(self, sender_username: str, message: str, sender_ip: str) -> None:
        self.after(0, lambda: self.append_chat_log(sender_username, message))

    def on_file_transfer_started(self, requester_username: str, file_name: str) -> None:
        self.after(0, lambda: self.append_chat_log("Hệ thống", f"{requester_username} đang tải {file_name}"))

    def on_file_transfer_completed(self, requester_username: str, file_name: str, bytes_sent: int) -> None:
        self.after(0, lambda: self.append_chat_log("Hệ thống", f"{requester_username} đã tải xong {file_name} ({bytes_sent} bytes)"))

    def on_file_transfer_failed(self, requester_username: str, file_name: str, reason: str) -> None:
        self.after(0, lambda: self.append_chat_log("Hệ thống", f"Tải file {file_name} thất bại: {reason}"))

    def on_close(self) -> None:
        if self.is_connected:
            self.disconnect()
        self.destroy()


if __name__ == "__main__":
    app = MainGUI()
    app.mainloop()
