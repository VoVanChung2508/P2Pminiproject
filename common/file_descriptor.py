from __future__ import annotations

import math
from typing import Optional


class FileDescriptor:
    """Metadata for a shared file in the P2P network."""

    def __init__(
        self,
        file_name: str = "",
        file_size: int = 0,
        owner_username: str = "",
        owner_ip: str = "",
        owner_p2p_port: int = 0,
    ) -> None:
        self.file_name = file_name
        self.file_size = int(file_size)
        self.owner_username = owner_username
        self.owner_ip = owner_ip
        self.owner_p2p_port = int(owner_p2p_port)

    def get_file_name(self) -> str:
        return self.file_name

    def set_file_name(self, value: str) -> None:
        self.file_name = value

    def get_file_size(self) -> int:
        return self.file_size

    def set_file_size(self, value: int) -> None:
        self.file_size = int(value)

    def get_owner_username(self) -> str:
        return self.owner_username

    def set_owner_username(self, value: str) -> None:
        self.owner_username = value

    def get_owner_ip(self) -> str:
        return self.owner_ip

    def set_owner_ip(self, value: str) -> None:
        self.owner_ip = value

    def get_owner_p2p_port(self) -> int:
        return self.owner_p2p_port

    def set_owner_p2p_port(self, value: int) -> None:
        self.owner_p2p_port = int(value)

    def to_protocol_string(self) -> str:
        return ";".join(
            [
                self.file_name,
                str(self.file_size),
                self.owner_username,
                self.owner_ip,
                str(self.owner_p2p_port),
            ]
        )

    @classmethod
    def from_protocol_string(cls, value: Optional[str]) -> Optional["FileDescriptor"]:
        if value is None or not str(value).strip():
            return None
        parts = str(value).split(";")
        if len(parts) < 5:
            return None
        try:
            file_name = parts[0]
            file_size = int(parts[1])
            owner_username = parts[2]
            owner_ip = parts[3]
            owner_p2p_port = int(parts[4])
            return cls(file_name, file_size, owner_username, owner_ip, owner_p2p_port)
        except (TypeError, ValueError):
            return None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FileDescriptor):
            return NotImplemented
        return (
            self.file_size == other.file_size
            and self.owner_p2p_port == other.owner_p2p_port
            and self.file_name == other.file_name
            and self.owner_username == other.owner_username
        )

    def __hash__(self) -> int:
        return hash((self.file_name, self.file_size, self.owner_username, self.owner_p2p_port))

    def __str__(self) -> str:
        return (
            f"{self.file_name} ({self.format_file_size(self.file_size)}) - "
            f"Chủ sở hữu: {self.owner_username} [{self.owner_ip}:{self.owner_p2p_port}]"
        )

    @staticmethod
    def format_file_size(bytes_value: int) -> str:
        if bytes_value < 1024:
            return f"{bytes_value} B"
        exp = int(math.log(bytes_value) / math.log(1024))
        pre = "KMGTPE"[exp - 1]
        value = bytes_value / (1024 ** exp)
        return f"{value:.1f} {pre}B"
