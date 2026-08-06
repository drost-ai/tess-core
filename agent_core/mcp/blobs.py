from __future__ import annotations

import base64
import hashlib
import os
import re
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..storage import getStateRoot
from .protocol import _sanitize_name
from .transport_shared import contextlib_suppress


async def _save_blob_content(server: str, uri: str, data: str) -> str:
    target_dir = Path(getStateRoot()) / "mcp-blobs" / _sanitize_name(server)
    target_dir.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(data)
    target = target_dir / _blob_file_name(uri, raw)
    target.write_bytes(raw)
    _cleanup_blob_dir(target_dir, keep=int(os.environ.get("AGENT_CORE_MCP_BLOB_KEEP") or 64))
    return str(target)


def _blob_file_name(uri: str, raw: bytes) -> str:
    parsed = urlparse(uri)
    basename = Path(unquote(parsed.path)).name or Path(unquote(uri)).name
    if not basename:
        basename = str(uuid.uuid5(uuid.NAMESPACE_URL, uri))
    ext = Path(basename).suffix
    if not ext or len(ext) > 16 or not re.fullmatch(r"\.[A-Za-z0-9._-]+", ext):
        ext = ".bin"
    stem = Path(basename).stem or _sanitize_name(uri)
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"{_sanitize_name(stem)}-{digest}{ext}"


def _cleanup_blob_dir(target_dir: Path, keep: int = 64) -> None:
    if keep <= 0:
        return
    try:
        files = [path for path in target_dir.iterdir() if path.is_file()]
    except FileNotFoundError:
        return
    for path in sorted(files, key=lambda item: item.stat().st_mtime_ns, reverse=True)[keep:]:
        with contextlib_suppress():
            path.unlink()
