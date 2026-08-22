import os

from . import oplog


class FileOpError(Exception):
    def __init__(self, code, message, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


def safe_move(src, dst):
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    if not os.path.isfile(src):
        raise FileOpError("src_missing", f"source not found: {src}")
    if os.path.exists(dst):
        raise FileOpError("dst_exists", f"destination exists: {dst}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except FileExistsError:
        raise FileOpError("dst_exists", f"destination exists: {dst}")
    os.remove(src)
    oplog.log("move", src=src, dst=dst)
    return dst
