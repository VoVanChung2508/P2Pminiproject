from __future__ import annotations

import socket
from typing import List

from common.file_descriptor import FileDescriptor
from common.message_protocol import (
    CMD_GET_ENDPOINT,
    CMD_GET_PEERS,
    CMD_LOGOUT,
    CMD_REGISTER,
    CMD_SEARCH_FILES,
    CMD_UPDATE_FILES,
    RES_ERROR,
    RES_ENDPOINT,
    RES_PEER_LIST,
    RES_REGISTER_ERR,
    RES_REGISTER_OK,
    RES_SEARCH_RESULTS,
    build_message,
)


class ClientHandler:
    def __init__(self, client_socket: socket.socket, server) -> None:
        self.socket = client_socket
        self.server = server
        self.peer_info = None
        self.running = True
        self.username = None

    def run(self) -> None:
        try:
            reader = self.socket.makefile("r", encoding="utf-8", newline="")
            while self.running:
                line = reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                self.handle_message(line)
        except Exception as exc:
            self.server.log(f"Kết nối bị ngắt đối với {self.username or 'Client'}: {exc}")
        finally:
            self.cleanup()

    def handle_message(self, raw_message: str) -> None:
        if raw_message is None or not raw_message.strip():
            return
        tokens = raw_message.split("|")
        command = tokens[0]

        if command == CMD_REGISTER:
            self.handle_register(tokens)
        elif command == CMD_UPDATE_FILES:
            self.handle_update_files(tokens)
        elif command == CMD_GET_PEERS:
            self.handle_get_peers()
        elif command == CMD_SEARCH_FILES:
            self.handle_search_files(tokens)
        elif command == CMD_GET_ENDPOINT:
            self.handle_get_endpoint(tokens)
        elif command == CMD_LOGOUT:
            self.running = False
        else:
            self.send_response(build_message(RES_ERROR, f"Lệnh không hợp lệ: {command}"))

    def handle_register(self, tokens) -> None:
        if len(tokens) < 3:
            self.send_response(build_message(RES_REGISTER_ERR, "Thiếu tham số đăng ký."))
            return

        username = tokens[1].strip()
        try:
            p2p_port = int(tokens[2].strip())
        except ValueError:
            self.send_response(build_message(RES_REGISTER_ERR, "Cổng P2P không hợp lệ."))
            return

        if not username:
            self.send_response(build_message(RES_REGISTER_ERR, "Username không được để trống."))
            return

        client_ip = self.socket.getpeername()[0] if self.socket.getpeername() else "127.0.0.1"
        success = self.server.register_peer(username, client_ip, p2p_port, self)
        if success:
            self.peer_info = self.server.get_peer(username)
            self.username = username
            self.send_response(build_message(RES_REGISTER_OK, f"Đăng ký thành công tài khoản: {username}"))
            self.server.broadcast_peer_list()
        else:
            self.send_response(build_message(RES_REGISTER_ERR, f"Tên tài khoản '{username}' đã có người sử dụng."))

    def handle_update_files(self, tokens) -> None:
        if self.peer_info is None:
            self.send_response(build_message(RES_ERROR, "Chưa đăng ký tài khoản."))
            return

        file_list = []
        if len(tokens) >= 2 and tokens[1]:
            for entry in tokens[1].split("#"):
                file_descriptor = FileDescriptor.from_protocol_string(entry)
                if file_descriptor is not None:
                    file_descriptor.set_owner_username(self.peer_info.get_username())
                    file_descriptor.set_owner_ip(self.peer_info.get_ip_address())
                    file_descriptor.set_owner_p2p_port(self.peer_info.get_p2p_port())
                    file_list.append(file_descriptor)

        self.peer_info.update_shared_files(file_list)
        self.server.log(f"Peer {self.peer_info.get_username()} đã cập nhật {len(file_list)} file chia sẻ.")

    def handle_get_peers(self) -> None:
        self.send_response(self.server.build_peer_list_response())

    def handle_search_files(self, tokens) -> None:
        query = tokens[1].strip().lower() if len(tokens) >= 2 else ""
        results = self.server.search_files(query, self.peer_info.get_username() if self.peer_info else "")
        payload = "#".join(item.to_protocol_string() for item in results)
        self.send_response(build_message(RES_SEARCH_RESULTS, payload))

    def handle_get_endpoint(self, tokens) -> None:
        if len(tokens) < 2:
            return
        target_user = tokens[1].strip()
        target_peer = self.server.get_peer(target_user)
        if target_peer is not None:
            self.send_response(
                build_message(
                    RES_ENDPOINT,
                    target_peer.get_username(),
                    target_peer.get_ip_address(),
                    str(target_peer.get_p2p_port()),
                )
            )
        else:
            self.send_response(build_message(RES_ERROR, f"Peer '{target_user}' không tồn tại hoặc offline."))

    def send_response(self, message: str) -> None:
        if self.socket is None:
            return
        try:
            self.socket.sendall((message + "\n").encode("utf-8"))
        except Exception as exc:
            self.server.log(f"Lỗi gửi response: {exc}")

    def cleanup(self) -> None:
        self.running = False
        if self.peer_info is not None:
            self.server.unregister_peer(self.peer_info.get_username())
            self.server.broadcast_peer_list()
        try:
            if self.socket is not None and not self.socket._closed:
                self.socket.close()
        except Exception:
            pass
