from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from common.file_descriptor import FileDescriptor


class SharedFileManager:
    def __init__(self, shared_directory) -> None:
        self.shared_directory = None
        self.set_shared_directory(shared_directory)

    def get_shared_directory(self):
        return self.shared_directory

    def set_shared_directory(self, directory) -> None:
        path = Path(directory) if directory is not None else None
        if path is not None and (not path.exists() or not path.is_dir()):
            path.mkdir(parents=True, exist_ok=True)
        self.shared_directory = path

    def scan_shared_files(self, owner_username: str, owner_ip: str, owner_p2p_port: int) -> List[FileDescriptor]:
        descriptors: List[FileDescriptor] = []
        if self.shared_directory is None or not self.shared_directory.exists():
            return descriptors

        for file_path in sorted(self.shared_directory.iterdir(), key=lambda item: item.name.lower()):
            if file_path.is_file() and not file_path.name.startswith("."):
                descriptors.append(
                    FileDescriptor(
                        file_name=file_path.name,
                        file_size=file_path.stat().st_size,
                        owner_username=owner_username,
                        owner_ip=owner_ip,
                        owner_p2p_port=owner_p2p_port,
                    )
                )
        return descriptors

    def get_file_by_name(self, file_name: str):
        if self.shared_directory is None or file_name is None:
            return None
        file_path = self.shared_directory / file_name
        if file_path.exists() and file_path.is_file():
            return file_path
        return None

    def open_file_input_stream(self, file_name: str):
        file_path = self.get_file_by_name(file_name)
        if file_path is None:
            raise FileNotFoundError(f"Không tìm thấy file '{file_name}' trong thư mục chia sẻ.")
        return open(file_path, "rb")
