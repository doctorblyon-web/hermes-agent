"""Fixed-endpoint, bounded HTTP client for the M3 dashboard projection."""

from __future__ import annotations

import http.client
import json
import socket
import time
from datetime import datetime, timezone
from typing import Any


HOST = "100.122.219.24"
PORT = 3851
PATH = "/api/state"
CONNECT_TIMEOUT_SECONDS = 2.0
TOTAL_TIMEOUT_SECONDS = 5.0
BODY_BYTES_MAX = 512 * 1024


class DashboardClientError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _decode_json(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DashboardClientError("duplicate_json_key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=reject_duplicates)
    except DashboardClientError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise DashboardClientError("invalid_json") from exc
    if not isinstance(value, dict):
        raise DashboardClientError("invalid_projection")
    return value


def fetch_state() -> tuple[dict[str, Any], str]:
    """GET the one fixed endpoint and return a completely read response."""
    started = time.monotonic()
    conn = http.client.HTTPConnection(HOST, PORT, timeout=CONNECT_TIMEOUT_SECONDS)
    try:
        conn.request(
            "GET",
            PATH,
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = conn.getresponse()
        if 300 <= response.status < 400:
            raise DashboardClientError("redirect_refused")
        if response.status != 200:
            raise DashboardClientError("http_error")
        length = response.getheader("Content-Length")
        if length is not None:
            try:
                if int(length) > BODY_BYTES_MAX:
                    raise DashboardClientError("response_too_large")
            except ValueError as exc:
                raise DashboardClientError("invalid_content_length") from exc

        chunks: list[bytes] = []
        size = 0
        while True:
            remaining = TOTAL_TIMEOUT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise DashboardClientError("timeout")
            if conn.sock is not None:
                conn.sock.settimeout(remaining)
            chunk = response.read(min(65536, BODY_BYTES_MAX + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > BODY_BYTES_MAX:
                raise DashboardClientError("response_too_large")
    except DashboardClientError:
        raise
    except (OSError, socket.timeout, http.client.HTTPException) as exc:
        raise DashboardClientError("unavailable") from exc
    finally:
        conn.close()

    projection_read_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return _decode_json(b"".join(chunks)), projection_read_at
