import os
import shutil

from . import oplog


class FileOpError(Exception):
    def __init__(self, code, message, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


def _same_filesystem(a, b):
    return os.stat(a).st_dev == os.stat(b).st_dev


def _copy_exclusive(src, dst):
    fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
        shutil.copystat(src, dst)
    except Exception:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise


def safe_move(src, dst):
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    if not os.path.isfile(src):
        raise FileOpError("src_missing", f"source not found: {src}")
    if os.path.exists(dst):
        raise FileOpError("dst_exists", f"destination exists: {dst}")
    dst_dir = os.path.dirname(dst)
    os.makedirs(dst_dir, exist_ok=True)
    linked = False
    if _same_filesystem(src, dst_dir):
        try:
            os.link(src, dst)
            linked = True
        except FileExistsError:
            raise FileOpError("dst_exists", f"destination exists: {dst}")
        except OSError:
            pass
    if not linked:
        try:
            _copy_exclusive(src, dst)
        except FileExistsError:
            raise FileOpError("dst_exists", f"destination exists: {dst}")
    os.remove(src)
    oplog.log("move", src=src, dst=dst)
    return dst
