from __future__ import annotations

from datetime import datetime
from typing import List


class PeerInfo:
    def __init__(self, username: str, ip_address: str, p2p_port: int) -> None:
        self.username = username
        self.ip_address = ip_address
        self.p2p_port = int(p2p_port)
        self.connected_at = datetime.now()
        self.shared_files = []

    def get_username(self) -> str:
        return self.username

    def get_ip_address(self) -> str:
        return self.ip_address

    def set_ip_address(self, value: str) -> None:
        self.ip_address = value

    def get_p2p_port(self) -> int:
        return self.p2p_port

    def set_p2p_port(self, value: int) -> None:
        self.p2p_port = int(value)

    def get_connected_at(self) -> datetime:
        return self.connected_at

    def get_shared_files(self) -> List:
        return self.shared_files

    def update_shared_files(self, new_files) -> None:
        self.shared_files = list(new_files) if new_files is not None else []

    def __str__(self) -> str:
        return (
            f"{self.username} ({self.ip_address}:{self.p2p_port}) - "
            f"Số file chia sẻ: {len(self.shared_files)}"
        )
