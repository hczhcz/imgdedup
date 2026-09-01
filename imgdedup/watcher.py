import ctypes
import os
import select
import struct

IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_ALL_EVENTS = (IN_MODIFY | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO
                 | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF)

_EVENT_FMT = "iIII"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
_O_CLOEXEC = 0o2000000
_O_NONBLOCK = 0o4000


class _Libc:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int
        self.libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self.libc.inotify_rm_watch.restype = ctypes.c_int

    def init(self):
        fd = self.libc.inotify_init1(_O_CLOEXEC | _O_NONBLOCK)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        return fd

    def add_watch(self, fd, path, mask):
        wd = self.libc.inotify_add_watch(fd, os.fsencode(path), mask)
        if wd < 0:
            raise OSError(ctypes.get_errno(), "inotify_add_watch")
        return wd

LIB = _Libc()


class Watcher:
    def __init__(self, root, ignored=None):
        self.root = os.path.abspath(root)
        self.ignored = ignored or (lambda p: False)
        self.fd = -1
        self.wds = {}
        self._closed = False

    def start(self):
        self.fd = LIB.init()
        self._watch_tree(self.root)

    def _watch_tree(self, base):
        stack = [base]
        while stack:
            d = stack.pop()
            if self.ignored(d):
                continue
            try:
                wd = LIB.add_watch(self.fd, d, IN_ALL_EVENTS)
            except OSError as e:
                if e.errno == 28:
                    raise
                continue
            self.wds[wd] = d
            try:
                entries = os.scandir(d)
            except OSError:
                continue
            with entries:
                for e in entries:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                    except OSError:
                        continue

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        self.wds = {}

    def readable(self, timeout):
        if self.fd < 0:
            return False
        try:
            r, _, _ = select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            return False
        return bool(r)

    def read(self):
        events = []
        try:
            raw = os.read(self.fd, 65536)
        except OSError:
            return events
        off = 0
        while off + _EVENT_SIZE <= len(raw):
            wd, mask, _cookie, name_len = struct.unpack_from(_EVENT_FMT, raw, off)
            off += _EVENT_SIZE
            name = raw[off:off + name_len]
            off += name_len
            name = name.split(b"\x00", 1)[0].decode("utf-8", "replace")
            events.append((wd, mask, name))
        return events


def reduce_events(watcher, events):
    rescan = False
    dirty = set()
    for wd, mask, name in events:
        if mask & IN_Q_OVERFLOW:
            rescan = True
            continue
        if mask & IN_IGNORED:
            continue
        base = watcher.wds.get(wd)
        if base is None:
            continue
        if name:
            path = os.path.join(base, name)
        else:
            path = base
        if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
            rescan = True
        if mask & (IN_MOVE_SELF | IN_DELETE_SELF):
            rescan = True
        elif mask & (IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO
                     | IN_MODIFY | IN_CLOSE_WRITE):
            if not (mask & IN_ISDIR):
                dirty.add(path)
    return rescan, dirty
