"""Local-only A.P.P.L.E.P.I.E. capture client; never opens a physical camera.

The response metadata is JSON, but image bytes are returned separately for
PGC's existing attachment transport. No image data is put into model-visible JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CAMERAS = frozenset({'board', 'monitor'})
MAX_RESPONSE_BYTES = 4096
MAX_JPEG_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 25.0


class CameraBridgeError(ValueError):
    """Missing app, malformed response, or unsafe/mismatched image artifact."""


@dataclass(frozen=True)
class CameraReply:
    metadata: dict[str, Any]
    image_name: str | None = None
    jpeg_data: bytes | None = None


def default_socket_path() -> Path:
    return Path.home() / 'Library' / 'Application Support' / 'APPLEPIE' / 'capture.sock'


def request_capture(
    camera: str,
    *,
    session_id: str,
    socket_path: Path | None = None,
    capture_directory: Path | None = None,
) -> CameraReply:
    if camera not in CAMERAS:
        raise CameraBridgeError('camera must be board or monitor')
    if not isinstance(session_id, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:._-]{0,127}', session_id) is None:
        raise CameraBridgeError('invalid PGC routing session identifier')
    path = socket_path or default_socket_path()
    request = json.dumps(
        {'version': 2, 'command': 'capture', 'camera': camera, 'session': session_id},
        separators=(',', ':'),
    ).encode('utf-8') + b'\n'

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(TIMEOUT_SECONDS)
        try:
            client.connect(str(path))
            client.sendall(request)
            response = bytearray()
            while len(response) <= MAX_RESPONSE_BYTES:
                chunk = client.recv(1)
                if not chunk:
                    raise CameraBridgeError('A.P.P.L.E.P.I.E. closed the socket without a complete response')
                if chunk == b'\n':
                    break
                response.extend(chunk)
            else:
                raise CameraBridgeError('A.P.P.L.E.P.I.E. response is too large')
        except (OSError, TimeoutError) as exc:
            raise CameraBridgeError(f'A.P.P.L.E.P.I.E. socket unavailable: {exc}') from exc

    try:
        metadata = json.loads(response)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CameraBridgeError('A.P.P.L.E.P.I.E. returned invalid JSON') from exc
    if not isinstance(metadata, dict) or metadata.get('version') != 2 or type(metadata.get('ok')) is not bool:
        raise CameraBridgeError('A.P.P.L.E.P.I.E. returned an unsupported response')
    if metadata.get('camera') not in (camera, None):
        raise CameraBridgeError('A.P.P.L.E.P.I.E. response selected a different camera')
    if not metadata['ok']:
        return CameraReply(metadata)

    if metadata.get('camera') != camera:
        raise CameraBridgeError('A.P.P.L.E.P.I.E. success response omitted the selected camera')
    filename = metadata.get('filename')
    expected_size = metadata.get('byte_count')
    expected_hash = metadata.get('sha256')
    width, height = metadata.get('width'), metadata.get('height')
    if not isinstance(filename, str) or not Path(filename).is_absolute():
        raise CameraBridgeError('A.P.P.L.E.P.I.E. returned an invalid capture path')
    if type(expected_size) is not int or not (1 <= expected_size <= MAX_JPEG_BYTES):
        raise CameraBridgeError('A.P.P.L.E.P.I.E. returned an invalid image size')
    if not isinstance(expected_hash, str) or re.fullmatch(r'[0-9a-f]{64}', expected_hash) is None:
        raise CameraBridgeError('A.P.P.L.E.P.I.E. returned an invalid image hash')
    if any(type(n) is not int or n <= 0 or n > 8192 for n in (width, height)):
        raise CameraBridgeError('A.P.P.L.E.P.I.E. returned invalid image dimensions')

    file = Path(filename)
    expected_directory = (capture_directory or default_socket_path().parent / 'captures').resolve()
    if file.parent.resolve() != expected_directory or file.is_symlink():
        raise CameraBridgeError('A.P.P.L.E.P.I.E. capture path is outside its private capture directory')
    if re.fullmatch(rf'{camera}-[0-9a-fA-F-]{{36}}\.jpg', file.name) is None:
        raise CameraBridgeError('A.P.P.L.E.P.I.E. returned an unexpected capture filename')

    try:
        fd = os.open(file, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
                raise CameraBridgeError('A.P.P.L.E.P.I.E. capture is not a regular file of the declared size')
            if info.st_uid != os.getuid():
                raise CameraBridgeError('A.P.P.L.E.P.I.E. capture belongs to a different user')
            chunks = []
            remaining = expected_size
            while remaining:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b''.join(chunks)
        finally:
            os.close(fd)
    except OSError as exc:
        raise CameraBridgeError(f'Cannot read A.P.P.L.E.P.I.E. capture: {exc}') from exc
    if len(data) != expected_size or not data.startswith(b'\xff\xd8\xff'):
        raise CameraBridgeError('A.P.P.L.E.P.I.E. capture is missing or is not JPEG data')
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise CameraBridgeError('A.P.P.L.E.P.I.E. capture SHA-256 does not match its metadata')
    return CameraReply(metadata, file.name, data)
