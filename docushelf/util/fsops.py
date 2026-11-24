"""File system primitives with crash safety in mind.

The rules every caller relies on:

* Nothing is ever overwritten by accident: new files are published with a hard
  link (which fails if the name exists) or an exclusive create.
* Writes of state files are atomic (temp file + fsync + rename).
* Moving across file systems copies, fsyncs and only then removes the source.
* A file that takes the place of another, or a copy that stands in for a
  moved file, carries over its permission bits and extended attributes (where
  Samba keeps Finder tags, comments and DOS attributes), never its times.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from ..errors import Conflict
from .names import OWN_TEMP_PREFIX, casefold_key, unique_filename

log = logging.getLogger(__name__)

_LINK_UNSUPPORTED = {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.EACCES, errno.ENOSYS}
# Extended attributes that cannot be copied for these reasons are skipped quietly:
# not supported by the target file system, reserved for privileged processes
# (security.*, trusted.*) or gone in the meantime.
_XATTR_SKIPPED = {
    code
    for code in (
        errno.EPERM,
        errno.EACCES,
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
        errno.EINVAL,
        getattr(errno, "ENODATA", None),
        getattr(errno, "ENOATTR", None),
    )
    if code is not None
}


def fsync_dir(path: Path) -> None:
    """Persist directory entries (renames, new files). Silently ignored where unsupported."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def temp_name_for(target: Path) -> Path:
    return target.parent / f"{OWN_TEMP_PREFIX}{uuid.uuid4().hex}.part"


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_name_for(path)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if mode is None else mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is None:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(path.parent)


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, obj: Any, *, mode: int | None = None) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n"
    atomic_write_text(path, text, mode=mode)


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_files(paths: list[Path]) -> str:
    """Hash of a single file, or a combined hash for a multi-file submission."""
    if len(paths) == 1:
        return sha256_file(paths[0])
    digest = hashlib.sha256()
    for path in paths:
        digest.update(sha256_file(path).encode("ascii"))
    return digest.hexdigest()


def file_signature(path: Path) -> tuple[int, int]:
    """(size, mtime_ns) – cheap change detection."""
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


def copy_xattrs(src: Path, dst: Path) -> int:
    """Copy the extended attributes of ``src`` to ``dst``; returns how many were copied.

    Samba with ``vfs objects = streams_xattr`` keeps Finder tags, colour
    labels, comments and DOS attributes in extended attributes of the file,
    so a file that replaces another has to take them over. Best effort:
    attributes the file system or the process may not write are skipped and
    nothing is raised. Without an xattr API in Python (macOS, Windows) this
    does nothing.
    """
    listxattr = getattr(os, "listxattr", None)
    getxattr = getattr(os, "getxattr", None)
    setxattr = getattr(os, "setxattr", None)
    if listxattr is None or getxattr is None or setxattr is None:
        return 0
    try:
        names = listxattr(src, follow_symlinks=False)
    except OSError as exc:
        if exc.errno not in _XATTR_SKIPPED:
            log.warning("cannot list the extended attributes of %s: %s", src, exc)
        return 0
    copied = 0
    for name in names:
        try:
            setxattr(dst, name, getxattr(src, name, follow_symlinks=False), follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in _XATTR_SKIPPED:
                log.warning("cannot copy the extended attribute %s of %s: %s", name, src, exc)
            continue
        copied += 1
    return copied


def copy_file_fsync(src: Path, dst: Path) -> None:
    """Copy into a new file (exclusive create) and fsync it.

    Copies permission bits and extended attributes, not the times.
    """
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with open(src, "rb") as source, os.fdopen(fd, "wb") as target:
            shutil.copyfileobj(source, target, 1 << 20)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        try:
            os.unlink(dst)
        except FileNotFoundError:
            pass
        raise
    try:
        shutil.copymode(src, dst)
    except OSError:
        pass
    copy_xattrs(src, dst)


def _place_no_clobber(src: Path, target: Path) -> None:
    """Make ``target`` a copy of ``src`` without ever replacing an existing ``target``.

    Raises FileExistsError if ``target`` exists.
    """
    try:
        os.link(src, target)
        return
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _LINK_UNSUPPORTED:
            raise
    # Different file system (or no hard links): stage a full copy next to the
    # target, then publish it under the final name.
    tmp = temp_name_for(target)
    copy_file_fsync(src, tmp)
    try:
        try:
            os.link(tmp, target)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno not in _LINK_UNSUPPORTED:
                raise
            if os.path.lexists(target):
                raise FileExistsError(errno.EEXIST, "target exists", str(target)) from None
            os.rename(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def publish_no_clobber(src: Path, dst_dir: Path, filename: str, *, move: bool) -> Path:
    """Put ``src`` into ``dst_dir`` as ``filename`` (or ``filename (n)``) and return the final path.

    Existing files are never replaced. With ``move=True`` the source is removed
    after the target is durable.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()
    for _ in range(10_000):
        name = unique_filename(dst_dir, filename, taken=taken)
        target = dst_dir / name
        try:
            _place_no_clobber(src, target)
        except FileExistsError:
            taken.add(casefold_key(name))
            continue
        fsync_dir(dst_dir)
        if move:
            os.unlink(src)
            fsync_dir(src.parent)
        return target
    raise Conflict(f"Kein freier Dateiname in {dst_dir}")


def place_exact(src: Path, target: Path, *, move: bool) -> None:
    """Like publish_no_clobber, but for one exact target name. Raises FileExistsError."""
    target.parent.mkdir(parents=True, exist_ok=True)
    _place_no_clobber(src, target)
    fsync_dir(target.parent)
    if move:
        os.unlink(src)
        fsync_dir(src.parent)


def move_file(src: Path, dst: Path) -> None:
    """Move a single file to an exact, currently unused path (cross-device safe)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "target exists", str(dst))
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _LINK_UNSUPPORTED:
            raise
        # Copy under a temporary name first: a crash mid-copy must never leave
        # a truncated file under the final name.
        tmp = temp_name_for(dst)
        try:
            copy_file_fsync(src, tmp)
            os.link(tmp, dst)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
    fsync_dir(dst.parent)
    os.unlink(src)
    fsync_dir(src.parent)


def move_dir(src: Path, dst: Path) -> None:
    """Move a directory to a path that must not exist yet."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "target exists", str(dst))
    try:
        os.rename(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copytree(src, dst)
        shutil.rmtree(src)
    fsync_dir(dst.parent)
    fsync_dir(src.parent)


def replace_if_unchanged(original: Path, replacement: Path, expected: tuple[int, int]) -> None:
    """Atomically swap ``replacement`` into ``original`` if ``original`` still matches ``expected``.

    ``replacement`` must live in the same directory. It takes over the
    permission bits and extended attributes of ``original`` (Finder tags and
    comments stored by Samba), but not its times: those tell later passes
    that the file changed. Raises Conflict when the original was modified
    since ``expected`` was taken.
    """
    # Copied first, so the check below happens right before the swap.
    try:
        shutil.copymode(original, replacement)
    except OSError:
        pass
    copy_xattrs(original, replacement)
    current = file_signature(original)
    if current != expected:
        raise Conflict(f"{original.name} wurde zwischenzeitlich verändert")
    os.replace(replacement, original)
    fsync_dir(original.parent)


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def dir_is_empty(path: Path) -> bool:
    with os.scandir(path) as entries:
        return next(entries, None) is None
