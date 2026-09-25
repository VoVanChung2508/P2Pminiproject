from __future__ import annotations

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common.message_protocol import (
    P2P_CMD_CHAT,
    P2P_CMD_FILE_REQ,
    P2P_RES_FILE_ERR,
    P2P_RES_FILE_OK,
    build_message,
)


class P2PServer:
    def __init__(self, port: int, file_manager, listener=None) -> None:
        self.port = int(port)
        self.file_manager = file_manager
        self.listener = listener
        self.server_socket = None
        self.running = False
        self.executor = ThreadPoolExecutor(max_workers=50)
        self.listen_thread = None

    def start(self) -> None:
        if self.running:
            return

        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("0.0.0.0", self.port))
        self.server_socket.listen(20)
        self.running = True
        self.listen_thread = threading.Thread(target=self.listen, daemon=True)
        self.listen_thread.start()

    def listen(self) -> None:
        while self.running and self.server_socket is not None:
            try:
                conn, _ = self.server_socket.accept()
                self.executor.submit(self.handle_incoming_peer_connection, conn)
            except OSError:
                if self.running:
                    continue
                break

    def handle_incoming_peer_connection(self, conn: socket.socket) -> None:
        try:
            reader = conn.makefile("r", encoding="utf-8", newline="")
            writer = conn.makefile("w", encoding="utf-8", newline="")
            first_line = reader.readline()
            if not first_line:
                return

            tokens = first_line.strip().split("|")
            command = tokens[0] if tokens else ""

            if command == P2P_CMD_CHAT and len(tokens) >= 3:
                sender = tokens[1]
                message = tokens[2]
                sender_ip = conn.getpeername()[0]
                if self.listener is not None:
                    self.listener.on_direct_message_received(sender, message, sender_ip)
            elif command == P2P_CMD_FILE_REQ and len(tokens) >= 3:
                requester = tokens[1]
                file_name = tokens[2]
                self.handle_file_transfer_request(requester, file_name, conn, writer)
        except Exception as exc:
            print(f"Lỗi xử lý kết nối P2P: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def handle_file_transfer_request(self, requester: str, file_name: str, conn: socket.socket, writer) -> None:
        file_path = self.file_manager.get_file_by_name(file_name)
        if file_path is None or not file_path.exists():
            writer.write(build_message(P2P_RES_FILE_ERR, "Không tìm thấy file trên máy chủ P2P này.") + "\n")
            writer.flush()
            if self.listener is not None:
                self.listener.on_file_transfer_failed(requester, file_name, "Không tìm thấy file")
            return

        file_size = file_path.stat().st_size
        if self.listener is not None:
            self.listener.on_file_transfer_started(requester, file_name)

        writer.write(build_message(P2P_RES_FILE_OK, file_name, str(file_size)) + "\n")
        writer.flush()

        bytes_sent = 0
        try:
            with open(file_path, "rb") as file_obj:
                while True:
                    chunk = file_obj.read(8192)
                    if not chunk:
                        break
                    conn.sendall(chunk)
                    bytes_sent += len(chunk)
            if self.listener is not None:
                self.listener.on_file_transfer_completed(requester, file_name, bytes_sent)
        except Exception as exc:
            if self.listener is not None:
                self.listener.on_file_transfer_failed(requester, file_name, str(exc))

    def stop(self) -> None:
        self.running = False
        try:
            if self.server_socket is not None:
                self.server_socket.close()
        except OSError:
            pass
        self.server_socket = None
        self.executor.shutdown(wait=False)
