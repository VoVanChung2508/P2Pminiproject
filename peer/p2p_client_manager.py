from __future__ import annotations

import socket
import threading
from concurrent.futures import ThreadPoolExecutor

from common.message_protocol import (
    P2P_CMD_FILE_REQ,
    P2P_RES_FILE_ERR,
    P2P_RES_FILE_OK,
    P2P_CMD_CHAT,
    build_message,
)


class P2PClientManager:
    def __init__(self) -> None:
        self.download_executor = ThreadPoolExecutor(max_workers=50)

    def send_direct_chat_message_async(self, target_ip: str, target_p2p_port: int, my_username: str, message: str, callback=None) -> None:
        def worker():
            try:
                with socket.create_connection((target_ip, int(target_p2p_port)), timeout=10) as sock:
                    writer = sock.makefile("w", encoding="utf-8", newline="")
                    writer.write(build_message(P2P_CMD_CHAT, my_username, message) + "\n")
                    writer.flush()
                if callback is not None:
                    callback.on_success()
            except Exception as exc:
                if callback is not None:
                    callback.on_failure(str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def download_file_async(self, target_ip: str, target_p2p_port: int, my_username: str, file_name: str, destination_dir, listener=None) -> None:
        def worker():
            destination_file = destination_dir / file_name if hasattr(destination_dir, "__truediv__") else destination_dir
            try:
                with socket.create_connection((target_ip, int(target_p2p_port)), timeout=20) as sock:
                    reader = sock.makefile("r", encoding="utf-8", newline="")
                    writer = sock.makefile("w", encoding="utf-8", newline="")
                    writer.write(build_message(P2P_CMD_FILE_REQ, my_username, file_name) + "\n")
                    writer.flush()

                    response_header = reader.readline()
                    if not response_header:
                        if listener is not None:
                            listener.on_download_failed(file_name, "Không nhận được phản hồi từ Peer.")
                        return

                    tokens = response_header.strip().split("|")
                    if tokens[0] == P2P_RES_FILE_ERR:
                        reason = tokens[1] if len(tokens) > 1 else "Lỗi phía máy chủ Peer"
                        if listener is not None:
                            listener.on_download_failed(file_name, reason)
                        return

                    if tokens[0] != P2P_RES_FILE_OK or len(tokens) < 3:
                        if listener is not None:
                            listener.on_download_failed(file_name, "Header giao thức sai định dạng: " + response_header.strip())
                        return

                    total_bytes = int(tokens[2])
                    with open(destination_file, "wb") as output_file:
                        bytes_downloaded = 0
                        last_update = __import__("time").time()
                        bytes_since_last_update = 0
                        while bytes_downloaded < total_bytes:
                            chunk = sock.recv(8192)
                            if not chunk:
                                break
                            output_file.write(chunk)
                            bytes_downloaded += len(chunk)
                            bytes_since_last_update += len(chunk)

                            now = __import__("time").time()
                            if (now - last_update) >= 0.25 or bytes_downloaded == total_bytes:
                                elapsed = max(now - last_update, 0.001)
                                speed = (bytes_since_last_update / 1024.0) / elapsed
                                if listener is not None:
                                    listener.on_progress_update(file_name, bytes_downloaded, total_bytes, speed)
                                last_update = now
                                bytes_since_last_update = 0

                    if bytes_downloaded >= total_bytes:
                        if listener is not None:
                            listener.on_download_complete(file_name, destination_file)
                    else:
                        if listener is not None:
                            listener.on_download_failed(file_name, f"Tải chưa hoàn tất ({bytes_downloaded}/{total_bytes} bytes)")

            except Exception as exc:
                if listener is not None:
                    listener.on_download_failed(file_name, str(exc))

        self.download_executor.submit(worker)
