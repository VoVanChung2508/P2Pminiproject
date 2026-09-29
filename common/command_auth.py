from __future__ import annotations

import hashlib
import hmac
import json
import re

_SIGNATURE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def sign_process_command(
    token: str,
    request_id: str,
    client_name: str,
    command: str,
    argument: str = "",
) -> str:
    if not token:
        raise ValueError("A command-signing token is required.")
    payload = json.dumps(
        [request_id, client_name.casefold(), command, argument],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(token.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_process_command(
    token: str,
    request_id: str,
    client_name: str,
    command: str,
    argument: str,
    signature: str,
) -> bool:
    if not token or not _SIGNATURE_PATTERN.fullmatch(signature):
        return False
    expected = sign_process_command(
        token,
        request_id,
        client_name,
        command,
        argument,
    )
    return hmac.compare_digest(expected, signature)
