from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List


@dataclass
class PeerInfo:
    username: str
    ip: str
    p2p_port: int
    shared_files: List[str] = field(default_factory=list)
    connected_at: datetime = field(default_factory=datetime.now)

    def get_username(self) -> str:
        return self.username

    def get_ip_address(self) -> str:
        return self.ip

    def get_p2p_port(self) -> int:
        return self.p2p_port

    def get_shared_files(self) -> List[str]:
        return self.shared_files

    def get_connected_at(self) -> datetime:
        return self.connected_at
