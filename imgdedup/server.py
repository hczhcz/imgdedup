import hashlib
import io
import json
import mimetypes
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageOps

from . import fileops, oplog

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
THUMB_SIZE = 128

_thumb_lock = threading.Lock()
_thumb_cache = {}


def make_thumbnail(abs_path):
    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    key = hashlib.md5(f"{abs_path}:{st.st_mtime}:{st.st_size}".encode()).hexdigest()
    with _thumb_lock:
        if key in _thumb_cache:
            return _thumb_cache[key]
    try:
        img = Image.open(abs_path)
        img = ImageOps.exif_transpose(img)
        img.thumbnail((THUMB_SIZE, THUMB_SIZE))
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        data = buf.getvalue()
    except Exception:
        return None
    with _thumb_lock:
        if len(_thumb_cache) > 2000:
            _thumb_cache.clear()
        _thumb_cache[key] = data
    return data


def image_dimensions(abs_path):
    try:
        with Image.open(abs_path) as img:
            return img.size
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    runtimes = {}

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, ctype, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "max-age=60")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, code, message, status=400, extra=None):
        payload = {"error": code, "message": message}
        if extra:
            payload.update(extra)
        self.send_json(payload, status)

    def parse_path(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        return parsed.path, qs

    def get_runtime(self, qs):
        name = qs.get("group", [None])[0]
        rt = self.runtimes.get(name)
        if rt is None:
            raise fileops.FileOpError("no_group", f"unknown group: {name}")
        return rt

    def resolve_image_path(self, rt, qs):
        loc = qs.get("loc", ["lib"])[0]
        rel = qs.get("path", [None])[0]
        if rel is None:
            raise fileops.FileOpError("bad_request", "missing path")
        if loc == "repo":
            kind = qs.get("kind", ["fuzzy"])[0]
            return rt.abs_repo(rel, kind)
        return rt.abs_lib(rel)

    def do_GET(self):
        try:
            path, qs = self.parse_path()
            if path == "/" or path == "/index.html":
                self.serve_static("index.html")
            elif path.startswith("/static/"):
                self.serve_static(path[len("/static/"):])
            elif path == "/api/groups":
                self.api_groups()
            elif path == "/api/state":
                rt = self.get_runtime(qs)
                self.send_json(rt.list_snapshot())
            elif path == "/api/dupgroup":
                rt = self.get_runtime(qs)
                gid = int(qs.get("id", ["0"])[0])
                snap = rt.group_snapshot(gid)
                if snap is None:
                    self.send_error_json("not_found", "group not found", 404)
                else:
                    self.send_json(snap)
            elif path == "/api/image":
                self.api_image(qs, thumb=False)
            elif path == "/api/thumb":
                self.api_image(qs, thumb=True)
            elif path == "/api/imageinfo":
                self.api_imageinfo(qs)
            elif path == "/api/file-state":
                self.api_file_state(qs)
            else:
                self.send_error_json("not_found", "not found", 404)
        except fileops.FileOpError as e:
            self.send_error_json(e.code, e.message, 400, e.extra)
        except BrokenPipeError:
            pass
        except Exception as e:
            oplog.error("http_error", path=self.path, error=repr(e))
            try:
                self.send_error_json("internal", repr(e), 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            path, qs = self.parse_path()
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            rt = self.get_runtime({"group": [body.get("group")]})
            if path == "/api/move_to_repo":
                kind = rt.act_move_to_repo(body["path"], body["md5"], body["repo_name"])
                self.send_json({"ok": True, "repo_kind": kind})
            elif path == "/api/restore":
                rt.act_restore(body["path"], body["md5"], body["repo_name"])
                self.send_json({"ok": True})
            elif path == "/api/move":
                rt.act_move(body["path"], body["md5"], body["target"])
                self.send_json({"ok": True})
            elif path == "/api/ignore":
                rt.act_ignore(body["md5s"])
                self.send_json({"ok": True})
            elif path == "/api/unignore":
                rt.act_unignore(body["md5s"])
                self.send_json({"ok": True})
            else:
                self.send_error_json("not_found", "not found", 404)
        except KeyError as e:
            self.send_error_json("bad_request", f"missing field: {e}", 400)
        except fileops.FileOpError as e:
            self.send_error_json(e.code, e.message, 400, e.extra)
        except BrokenPipeError:
            pass
        except Exception as e:
            oplog.error("http_error", path=self.path, error=repr(e))
            try:
                self.send_error_json("internal", repr(e), 500)
            except Exception:
                pass

    def serve_static(self, name):
        safe = os.path.normpath(name)
        if safe.startswith("..") or os.path.isabs(safe):
            self.send_error_json("bad_path", "invalid path", 400)
            return
        fp = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(fp):
            self.send_error_json("not_found", "not found", 404)
            return
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        with open(fp, "rb") as f:
            self.send_bytes(f.read(), ctype, cache=False)

    def api_groups(self):
        groups = []
        for name, rt in self.runtimes.items():
            groups.append({
                "name": name,
                "library_root": rt.cfg.library_root,
                "dup_repo": rt.cfg.dup_repo,
                "keep_no_gap": rt.cfg.keep_no_gap,
                "min_level": rt.cfg.min_level,
                "version": rt.version,
            })
        self.send_json({"groups": groups})

    def api_image(self, qs, thumb):
        rt = self.get_runtime(qs)
        ap = self.resolve_image_path(rt, qs)
        if not os.path.isfile(ap):
            self.send_error_json("not_found", "image not found", 404)
            return
        if thumb:
            data = make_thumbnail(ap)
            if data is None:
                self.send_error_json("thumb_failed", "cannot thumbnail", 500)
                return
            self.send_bytes(data, "image/jpeg", cache=True)
        else:
            ctype = mimetypes.guess_type(ap)[0] or "application/octet-stream"
            with open(ap, "rb") as f:
                self.send_bytes(f.read(), ctype, cache=True)

    def api_imageinfo(self, qs):
        rt = self.get_runtime(qs)
        ap = self.resolve_image_path(rt, qs)
        if not os.path.isfile(ap):
            self.send_error_json("not_found", "image not found", 404)
            return
        st = os.stat(ap)
        dims = image_dimensions(ap)
        self.send_json({
            "size": st.st_size,
            "mtime": st.st_mtime,
            "width": dims[0] if dims else None,
            "height": dims[1] if dims else None,
        })

    def api_file_state(self, qs):
        rt = self.get_runtime(qs)
        rel = qs.get("path", [None])[0]
        md5 = qs.get("md5", [None])[0]
        repo_name = qs.get("repo_name", [None])[0]
        if rel is None:
            raise fileops.FileOpError("bad_request", "missing path")
        lib = rt.abs_lib(rel)
        lib_size = lib_mtime = None
        try:
            st = os.stat(lib)
            lib_size, lib_mtime = st.st_size, st.st_mtime
        except OSError:
            pass
        with rt.lock:
            lib_md5 = rt.get_path_md5(lib) if lib_size is not None else None
            matches = rt.repo_matches(repo_name, md5) if repo_name else []
        repo_size = repo_mtime = None
        if len(matches) == 1:
            try:
                st = os.stat(matches[0][1])
                repo_size, repo_mtime = st.st_size, st.st_mtime
            except OSError:
                pass
        self.send_json({
            "in_library": lib_md5 is not None and lib_md5 == md5,
            "path_occupied": lib_size is not None,
            "lib_md5": lib_md5,
            "in_repo": len(matches) == 1,
            "repo_kind": matches[0][0] if len(matches) == 1 else None,
            "lib_size": lib_size, "lib_mtime": lib_mtime,
            "repo_size": repo_size, "repo_mtime": repo_mtime,
        })


def run_server(app_cfg, runtimes):
    Handler.runtimes = runtimes
    server = ThreadingHTTPServer(("127.0.0.1", app_cfg.port), Handler)
    oplog.log("server_started", port=app_cfg.port)
    server.serve_forever()
