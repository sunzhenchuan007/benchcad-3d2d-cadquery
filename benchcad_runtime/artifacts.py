"""Small bounded artifact boundary; callers stop all workers before reading."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import stat

MAX_CAD_BYTES = 64 * 1024 * 1024
MAX_PNG_BYTES = 12 * 1024 * 1024


def read_regular(root: Path, relative: str | Path, limit: int, *, allow_empty: bool = False) -> bytes:
    """Reject links, traversal, special files, and oversized data before reading."""
    root = Path(root).resolve(strict=True)
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or any(p == '..' for p in relative.parts):
        raise ValueError('Artifact must be a relative path without traversal')
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Artifact links/reparse points are forbidden')
    if not candidate.resolve(strict=True).is_relative_to(root):
        raise ValueError('Artifact escaped its root')
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Artifact must be a single-link regular file')
    if not (0 if allow_empty else 1) <= info.st_size <= limit:
        raise ValueError('Artifact is empty or exceeds its size limit')
    fd = os.open(candidate, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
            raise ValueError('Artifact changed during open')
        data = stream.read(limit + 1)
    if not (0 if allow_empty else 1) <= len(data) <= limit:
        raise ValueError('Artifact is empty or exceeds its size limit')
    return data


def validate_png(data: bytes) -> tuple[int, int]:
    from PIL import Image
    if len(data) > MAX_PNG_BYTES or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Expected a PNG of at most 12 MiB')
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if (img.format != 'PNG' or not 0 < width <= 4096 or not 0 < height <= 4096
                    or width * height > 16_000_000 or getattr(img, 'n_frames', 1) != 1):
                raise ValueError('PNG dimensions/frame count exceed observation limits')
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
    except (Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise ValueError('PNG could not be safely decoded') from exc
    return width, height


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_new(path: Path, data: bytes, readonly: bool = False):
    """Exclusive writes never follow a pre-existing destination symlink."""
    with Path(path).open('xb') as stream:
        stream.write(data)
    if readonly:
        Path(path).chmod(0o444)


def snapshot_png(data: bytes, directory: Path) -> Path:
    validate_png(data)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (sha256(data) + '.png')
    if path.exists():
        if read_regular(directory, path.name, MAX_PNG_BYTES) != data:
            raise ValueError('Immutable observation hash mismatch')
    else:
        write_new(path, data, readonly=True)
    return path
