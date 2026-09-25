from __future__ import annotations

import socket
import threading
import json
from typing import TYPE_CHECKING

from common.message_protocol import (
    CMD_GET_ENDPOINT,
    CMD_GET_PEERS,
    CMD_LOGOUT,
    CMD_REGISTER,
    CMD_SEARCH_FILES,
    CMD_UPDATE_FILES,
    RES_ERROR,
    RES_PEER_LIST,
    RES_SEARCH_RESULTS,
)

if TYPE_CHECKING:
    from server.server_main import ServerMain


class ClientHandler:
    def __init__(self, client_socket: socket.socket, server: "ServerMain") -> None:
        self.client_socket = client_socket
        self.server = server
        self.running = True
        self.lock = threading.Lock()

    def send_response(self, message: str) -> None:
        if self.client_socket:
            try:
                self.client_socket.sendall(message.encode("utf-8"))
            except OSError:
                self.running = False

    def run(self) -> None:
        try:
            self.client_socket.settimeout(5)
            while self.running:
                try:
                    data = self.client_socket.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break

                message = data.decode("utf-8", errors="ignore").strip()
                if not message:
                    continue

                self.handle_message(message)
        except OSError:
            pass
        finally:
            self.close()

    def handle_message(self, message: str) -> None:
        if message.startswith("{"):
            self.handle_json_message(message)
            return

        parts = message.split("|")
        if not parts:
            return

        command = parts[0]
        if command == CMD_REGISTER:
            if len(parts) < 4:
                self.send_response(f"{RES_ERROR}|Invalid register payload")
                return
            username = parts[1]
            ip = parts[2]
            port = int(parts[3])
            files = parts[4:] if len(parts) > 4 else []
            if self.server.register_peer(username, ip, port, self):
                self.send_response(f"{RES_PEER_LIST}|{self.server.build_peer_list_response().split('|', 1)[1] if '|' in self.server.build_peer_list_response() else ''}")
            else:
                self.send_response(f"{RES_ERROR}|Username already exists")

        elif command == CMD_GET_PEERS:
            self.send_response(self.server.build_peer_list_response())

        elif command == CMD_SEARCH_FILES:
            query = parts[1] if len(parts) > 1 else ""
            requester = parts[2] if len(parts) > 2 else ""
            result = self.server.search_files(query, requester)
            payload = "#".join(
                f"{fd.get_file_name()};{fd.get_ip_address()};{fd.get_p2p_port()}" for fd in result
            )
            self.send_response(f"{RES_SEARCH_RESULTS}|{payload}")

        elif command == CMD_LOGOUT:
            username = parts[1] if len(parts) > 1 else None
            self.server.unregister_peer(username)
            self.send_response(f"{RES_ERROR}|Logged out")
            self.running = False

        else:
            self.send_response(f"{RES_ERROR}|Unsupported command: {command}")

    def handle_json_message(self, message: str) -> None:
        try:
            request = json.loads(message)
            action = request.get("action")
            payload = request.get("payload", {})
        except (json.JSONDecodeError, AttributeError):
            self.send_response(json.dumps({"status": "error", "message": "Invalid JSON request"}))
            return

        if action == "REGISTER":
            username = str(payload.get("name", "")).strip()
            ip = str(payload.get("host", "")).strip()
            port = int(payload.get("port", 0))
            if not username or not ip or not port:
                response = {"status": "error", "message": "Invalid register payload"}
            elif self.server.register_peer(username, ip, port, self):
                response = {"status": "ok", "peers": self.server.peer_list_for_json()}
            else:
                response = {"status": "error", "message": "Username already exists"}
        elif action == "LIST_PEERS":
            response = {"status": "ok", "peers": self.server.peer_list_for_json()}
        elif action == "DISCONNECT":
            username = str(payload.get("name", "")).strip()
            self.server.unregister_peer(username)
            response = {"status": "ok"}
            self.running = False
        elif action == "PING":
            response = {"status": "ok", "message": "pong"}
        else:
            response = {"status": "error", "message": f"Unsupported action: {action}"}

        self.send_response(json.dumps(response, ensure_ascii=False))

    def close(self) -> None:
        try:
            self.client_socket.close()
        except OSError:
            pass
        self.running = False
