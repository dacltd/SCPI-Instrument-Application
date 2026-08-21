from __future__ import annotations

import threading

from dmm_app.transport import BinaryResponseError, Transport


class IEEEBinaryBlockError(ValueError):
    """An IEEE 488.2 block was malformed, empty, or interrupted."""


class SCPIClient:
    def __init__(self, transport: Transport, terminator: str = "\n", encoding: str = "ascii"):
        self._transport = transport
        self._terminator = terminator
        self._encoding = encoding
        self._lock = threading.Lock()

    def write(self, command: str) -> None:
        payload = f"{command}{self._terminator}".encode(self._encoding)
        with self._lock:
            self._transport.write(payload)

    def query(self, command: str) -> str:
        response = self.query_raw(command)
        return response.decode(self._encoding, errors="replace").strip()

    def query_raw(self, command: str) -> bytes:
        payload = f"{command}{self._terminator}".encode(self._encoding)
        with self._lock:
            self._transport.write(payload)
            response = self._transport.read_until(self._terminator.encode(self._encoding))
        return response

    def query_binary_block(self, command: str) -> bytes:
        payload = f"{command}{self._terminator}".encode(self._encoding)
        with self._lock:
            self._transport.write(payload)
            try:
                response = self._transport.read_binary_response()
            except BinaryResponseError as exc:
                raise IEEEBinaryBlockError(str(exc)) from exc
        return decode_ieee_binary_blocks(response)

    def recover_binary_transfer(self) -> None:
        with self._lock:
            self._transport.recover_binary_response()


def decode_ieee_binary_blocks(response: bytes) -> bytes:
    """Extract and join one or more IEEE 488.2 definite-length data blocks."""
    remaining = response.lstrip(b"\r\n ")
    blocks: list[bytes] = []
    while remaining:
        if not remaining.startswith(b"#") or len(remaining) < 2:
            if blocks and not remaining.strip(b"\r\n"):
                break
            preview = remaining[:16].hex(" ")
            raise IEEEBinaryBlockError(
                "Response does not contain a valid IEEE binary block header "
                f"({len(remaining):,} unexpected byte(s), prefix: {preview or 'empty'})."
            )
        digits_byte = remaining[1:2]
        if not digits_byte.isdigit():
            raise IEEEBinaryBlockError("Invalid IEEE binary block length digit.")
        length_digits = int(digits_byte)
        if length_digits == 0:
            raise IEEEBinaryBlockError("Indefinite-length IEEE binary blocks are not supported.")
        header_end = 2 + length_digits
        if len(remaining) < header_end:
            raise IEEEBinaryBlockError("Incomplete IEEE binary block header.")
        length_field = remaining[2:header_end]
        if not length_field.isdigit():
            raise IEEEBinaryBlockError("Invalid IEEE binary block byte count.")
        payload_length = int(length_field)
        if payload_length == 0:
            raise IEEEBinaryBlockError("IEEE binary block payload is empty.")
        payload_end = header_end + payload_length
        if len(remaining) < payload_end:
            raise IEEEBinaryBlockError(
                f"Incomplete IEEE binary block: expected {payload_length} payload bytes, "
                f"received {len(remaining) - header_end}."
            )
        blocks.append(remaining[header_end:payload_end])
        remaining = remaining[payload_end:].lstrip(b"\r\n")
    if not blocks:
        raise IEEEBinaryBlockError("No IEEE binary block was returned.")
    return b"".join(blocks)
