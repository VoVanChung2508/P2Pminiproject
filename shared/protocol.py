import json
from typing import Any, Dict, Optional


DELIMITER = "|"


class ProtocolError(ValueError):
    """Raised when a message payload is invalid."""


def encode_message(action: str, payload: Optional[Dict[str, Any]] = None) -> str:
    """Serialize a message to protocol format."""
    message = {"action": action, "payload": payload or {}}
    return json.dumps(message, ensure_ascii=False)


def decode_message(raw: str) -> Dict[str, Any]:
    """Deserialize a protocol message."""
    if not raw or not isinstance(raw, str):
        raise ProtocolError("Empty message")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Invalid JSON payload: {exc}") from exc

    if not isinstance(data, dict):
        raise ProtocolError("Message must be a JSON object")

    action = data.get("action")
    if not action:
        raise ProtocolError("Message has no action")

    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError("Payload must be a dictionary")

    return {"action": action, "payload": payload}


def build_simple_message(action: str, **kwargs: Any) -> str:
    return encode_message(action, kwargs)


def parse_response(raw: str) -> Dict[str, Any]:
    return decode_message(raw)
