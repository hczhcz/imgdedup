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


def safe_move(src, dst):
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    if not os.path.isfile(src):
        raise FileOpError("src_missing", f"source not found: {src}")
    if os.path.exists(dst):
        raise FileOpError("dst_exists", f"destination exists: {dst}")
    dst_dir = os.path.dirname(dst)
    os.makedirs(dst_dir, exist_ok=True)
    if _same_filesystem(src, dst_dir):
        try:
            os.link(src, dst)
        except FileExistsError:
            raise FileOpError("dst_exists", f"destination exists: {dst}")
        os.remove(src)
    else:
        tmp = dst + ".imgdedup-tmp"
        if os.path.exists(tmp):
            raise FileOpError("tmp_exists", f"temp file exists: {tmp}")
        try:
            shutil.copy2(src, tmp)
            os.link(tmp, dst)
            os.remove(tmp)
        except FileExistsError:
            os.remove(tmp)
            raise FileOpError("dst_exists", f"destination exists: {dst}")
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        os.remove(src)
    oplog.log("move", src=src, dst=dst)
    return dst
