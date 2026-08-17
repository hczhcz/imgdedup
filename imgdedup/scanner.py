import fnmatch
import hashlib
import json
import os
import threading
import time

from . import czkawka, fileops, oplog
from .config import LEVELS

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".avif", ".jxl", ".heic"}


def is_image(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class GroupRuntime:
    def __init__(self, app_cfg, group_cfg):
        self.app_cfg = app_cfg
        self.cfg = group_cfg
        self.lock = threading.RLock()
        self.version = 0
        self.groups = {}
        self.file_index = {}
        self.md5_cache = {}
        self.repo_md5_cache = {}
        self.czkawka_groups = []
        self.tree_signature = None
        self.last_tree_change = 0.0
        self.ignored_sets = set()
        self.state_path = os.path.join(app_cfg.state_dir, f"{group_cfg.name}.json")
        self.stop_event = threading.Event()
        self.czkawka_wakeup = threading.Event()
        self.first_scan_done = threading.Event()
        self.load_state()

    def load_state(self):
        if not os.path.isfile(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.ignored_sets = {frozenset(x) for x in data.get("ignored", []) if x}
            oplog.log("state_loaded", group=self.cfg.name, ignored=len(self.ignored_sets))
        except Exception as e:
            oplog.error("state_load_failed", group=self.cfg.name, error=str(e))

    def save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with self.lock:
            data = {"ignored": sorted(sorted(s) for s in self.ignored_sets)}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)

    def bump(self):
        self.version += 1

    def rel(self, abs_path):
        return os.path.relpath(abs_path, self.cfg.library_root)

    def abs_lib(self, rel_path):
        p = os.path.abspath(os.path.join(self.cfg.library_root, rel_path))
        if not (p == self.cfg.library_root or p.startswith(self.cfg.library_root.rstrip(os.sep) + os.sep)):
            raise fileops.FileOpError("bad_path", f"path escapes library: {rel_path}")
        return p

    def repo_root(self, kind):
        return self.cfg.exact_dup_repo if kind == "exact" else self.cfg.dup_repo

    def abs_repo(self, repo_name, kind="fuzzy"):
        root = self.repo_root(kind)
        p = os.path.abspath(os.path.join(root, repo_name))
        if not (p == root or p.startswith(root.rstrip(os.sep) + os.sep)):
            raise fileops.FileOpError("bad_path", f"path escapes repo: {repo_name}")
        return p

    def excluded(self, abs_path):
        for pat in self.cfg.exclude_patterns:
            if fnmatch.fnmatch(abs_path, pat):
                return True
        return False

    def walk_files(self):
        result = {}
        root = self.cfg.library_root
        repos = [self.cfg.dup_repo.rstrip(os.sep), self.cfg.exact_dup_repo.rstrip(os.sep)]
        for dirpath, dirnames, filenames in os.walk(root):
            ad = os.path.abspath(dirpath)
            if any(ad == r or ad.startswith(r + os.sep) for r in repos):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in sorted(dirnames) if not self.excluded(os.path.join(dirpath, d))]
            for fn in sorted(filenames):
                ap = os.path.join(dirpath, fn)
                if not is_image(ap) or self.excluded(ap):
                    continue
                try:
                    st = os.stat(ap)
                except OSError:
                    continue
                if st.st_size < self.cfg.min_file_size:
                    continue
                result[self.rel(ap)] = (st.st_size, st.st_mtime, st.st_ctime)
        return result

    def get_md5(self, rp, file_index):
        if rp not in file_index:
            return None
        size, mtime, _ = file_index[rp]
        key = (rp, size, mtime)
        md5 = self.md5_cache.get(key)
        if md5 is None:
            try:
                md5 = file_md5(self.abs_lib(rp))
            except OSError:
                return None
            self.md5_cache[key] = md5
        return md5

    def evict_md5_cache(self, file_index):
        stale = [k for k in self.md5_cache
                 if k[0] not in file_index or file_index[k[0]][:2] != (k[1], k[2])]
        for k in stale:
            del self.md5_cache[k]

    def get_path_md5(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (st.st_size, st.st_mtime)
        entry = self.repo_md5_cache.get(path)
        if entry is not None and entry[0] == key:
            return entry[1]
        try:
            md5 = file_md5(path)
        except OSError:
            return None
        self.repo_md5_cache[path] = (key, md5)
        return md5

    def merge_groups(self, czkawka_groups, file_index):
        parent = {}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            for x in (a, b):
                if x not in parent:
                    parent[x] = x
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        def current(item):
            entry = file_index.get(item["path"])
            if entry is None:
                return False
            size, mtime, _ = entry
            return size == item["size"] and abs(mtime - item["mtime"]) < 2

        similarity_of = {}
        for g in czkawka_groups:
            if not current(g[0]):
                continue
            base = g[0]["path"]
            for item in g[1:]:
                if not current(item):
                    continue
                rp = item["path"]
                union(base, rp)
                sim = item["similarity"]
                if rp not in similarity_of or sim < similarity_of[rp]:
                    similarity_of[rp] = sim

        components = {}
        for x in parent:
            components.setdefault(find(x), set()).add(x)

        scanned = []
        for members in components.values():
            if len(members) < 2:
                continue
            sims = [similarity_of[m] for m in members if m in similarity_of]
            best = min(sims) if sims else None
            level = czkawka.classify_similarity(best, self.cfg.czkawka_hash_size) if best is not None else None
            if level is None:
                continue
            if LEVELS.index(level) < self.cfg.min_level_index():
                continue
            scanned.append({"members": sorted(members), "level": level})
        return scanned

    def reconcile(self, scanned, file_index):
        with self.lock:
            groups = {}
            for sg in scanned:
                members = sg["members"]
                if len(members) < 2:
                    continue
                identity = "\0".join(members)
                gid = int(hashlib.sha256(identity.encode()).hexdigest()[:13], 16)
                old = self.groups.get(gid)
                groups[gid] = {
                    "id": gid,
                    "files": members,
                    "level": sg["level"],
                    "created_at": old["created_at"] if old else time.time(),
                }
            before = {(gid, g["level"], tuple(g["files"])) for gid, g in self.groups.items()}
            after = {(gid, g["level"], tuple(g["files"])) for gid, g in groups.items()}
            changed = before != after
            self.groups = groups
            if changed:
                self.bump()
            return changed

    def group_md5_set(self, g):
        md5s = set()
        for rp in g["files"]:
            md5 = self.get_md5(rp, self.file_index)
            if md5 is None:
                return None
            md5s.add(md5)
        return frozenset(md5s) if md5s else None

    def is_ignored(self, g):
        if not self.ignored_sets:
            return False
        s = self.group_md5_set(g)
        return s is not None and s in self.ignored_sets

    def group_snapshot(self, gid):
        with self.lock:
            g = self.groups.get(gid)
            if not g:
                return None
            files = []
            md5_counts = {}
            order = sorted(g["files"])
            last = order[-1] if order else None
            dup_dirs = {}
            for other in self.groups.values():
                for rp in other["files"]:
                    dup_dirs.setdefault(os.path.dirname(rp), {})[os.path.basename(rp)] = other["id"]
            for rp in order:
                info = {
                    "rel_path": rp,
                    "md5": self.get_md5(rp, self.file_index),
                    "status": "present" if rp in self.file_index else "missing",
                    "is_last": rp == last,
                    "size": None, "mtime": None,
                    "neighbors_prev": [], "neighbors_next": [],
                }
                if info["md5"] is not None:
                    md5_counts[info["md5"]] = md5_counts.get(info["md5"], 0) + 1
                ap = self.abs_lib(rp)
                if os.path.isfile(ap):
                    try:
                        st = os.stat(ap)
                        info["size"] = st.st_size
                        info["mtime"] = st.st_mtime
                    except OSError:
                        pass
                d = os.path.dirname(rp)
                dir_dups = dup_dirs.get(d, {})
                siblings = self._dir_siblings(d)
                base = os.path.basename(rp)
                if base in siblings:
                    idx = siblings.index(base)
                else:
                    idx = 0
                    while idx < len(siblings) and siblings[idx] < base:
                        idx += 1
                    siblings = siblings[:idx] + [None] + siblings[idx:]
                for s in siblings[max(0, idx - 5):idx]:
                    if s is not None:
                        rp2 = os.path.join(d, s) if d else s
                        info["neighbors_prev"].append(
                            {"rel_path": rp2, "dup_gid": dir_dups.get(s)})
                for s in siblings[idx + 1:idx + 6]:
                    if s is not None:
                        rp2 = os.path.join(d, s) if d else s
                        info["neighbors_next"].append(
                            {"rel_path": rp2, "dup_gid": dir_dups.get(s)})
                files.append(info)
            level = "Exact" if any(n >= 2 for n in md5_counts.values()) else g["level"]
            return {
                "id": gid,
                "level": level,
                "ignored": self.is_ignored(g),
                "keep_no_gap": self.cfg.keep_no_gap,
                "files": files,
            }

    def _dir_siblings(self, rel_dir):
        try:
            abs_dir = self.abs_lib(rel_dir)
            names = [n for n in os.listdir(abs_dir)
                     if is_image(n) and os.path.isfile(os.path.join(abs_dir, n))
                     and not self.excluded(os.path.join(abs_dir, n))]
            return sorted(names)
        except OSError:
            return []

    def is_old(self, g):
        if self.cfg.hide_before is None:
            return False
        mtimes = [max(self.file_index[rp][1], self.file_index[rp][2])
                  for rp in g["files"] if rp in self.file_index]
        return bool(mtimes) and all(m < self.cfg.hide_before for m in mtimes)

    def list_snapshot(self):
        with self.lock:
            items = []
            ignored = []
            old = []
            for gid in sorted(self.groups.keys()):
                g = self.groups[gid]
                group_md5s = None
                if self.ignored_sets or g["level"] == "Original":
                    group_md5s = self.group_md5_set(g)
                ignored_set = group_md5s if group_md5s in self.ignored_sets else None
                exact = (group_md5s is not None
                         and g["level"] == "Original"
                         and len(group_md5s) < len(g["files"]))
                if ignored_set is not None:
                    item = self._list_item(gid, g, exact)
                    item["ignored_md5s"] = sorted(ignored_set)
                    ignored.append(item)
                    continue
                if self.is_old(g):
                    old.append(self._list_item(gid, g, exact))
                    continue
                items.append(self._list_item(gid, g, exact))
            key = lambda g: max(f["rel_path"] for f in g["files"])
            items.sort(key=key)
            ignored.sort(key=key)
            old.sort(key=key)
            return {"version": self.version, "groups": items,
                    "ignored": ignored, "old": old}

    def _list_item(self, gid, g, exact=False):
        files = []
        for rp in sorted(g["files"]):
            files.append({
                "rel_path": rp,
                "name": os.path.basename(rp),
                "size": self.file_index[rp][0] if rp in self.file_index else None,
                "status": "present" if rp in self.file_index else "missing",
            })
        return {
            "id": gid,
            "level": "Exact" if exact else g["level"],
            "created_at": g["created_at"],
            "files": files,
        }

    def validate_library_file(self, rel_path, md5):
        path = self.abs_lib(rel_path)
        try:
            if file_md5(path) != md5:
                raise fileops.FileOpError("file_changed", "library file does not match path and md5")
        except OSError:
            raise fileops.FileOpError("file_changed", "library file does not match path and md5")
        return path

    def repo_matches(self, repo_name, md5):
        matches = []
        for kind in ("fuzzy", "exact"):
            path = self.abs_repo(repo_name, kind)
            if os.path.isfile(path) and self.get_path_md5(path) == md5:
                matches.append((kind, path))
        return matches

    def fuzzy_has_md5(self, md5, size):
        for dirpath, _, filenames in os.walk(self.cfg.dup_repo):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(path) != size:
                        continue
                except OSError:
                    continue
                if self.get_path_md5(path) == md5:
                    return True
        return False

    def library_has_md5(self, md5, size, exclude=None):
        with self.lock:
            candidates = [rp for rp, (sz, _, _) in self.file_index.items()
                          if sz == size and rp != exclude]
        for rp in candidates:
            if self.get_md5(rp, self.file_index) == md5:
                return True
        return False

    def act_move_to_repo(self, rel_path, md5, repo_name):
        with self.lock:
            name = repo_name or os.path.basename(rel_path)
            if os.sep in name or name in ("", ".", ".."):
                raise fileops.FileOpError("bad_name", f"invalid repo name: {name}")
            src = self.validate_library_file(rel_path, md5)
            size = os.path.getsize(src)
            kind = "exact" if (self.library_has_md5(md5, size, rel_path)
                               or self.fuzzy_has_md5(md5, size)) else "fuzzy"
            dst = self.abs_repo(name, kind)
            if os.path.exists(dst):
                raise fileops.FileOpError("dst_exists", name, {"repo_kind": kind})
            fileops.safe_move(src, dst)
            self.file_index.pop(rel_path, None)
            oplog.log("action_move_to_repo", group=self.cfg.name, file=rel_path,
                      md5=md5, repo_name=name, repo_kind=kind)
            self._after_action()
            return kind

    def act_restore(self, rel_path, md5, repo_name):
        with self.lock:
            matches = self.repo_matches(repo_name, md5)
            if len(matches) != 1:
                raise fileops.FileOpError("repo_file_ambiguous", "repository file not uniquely identified")
            kind, src = matches[0]
            dst = self.abs_lib(rel_path)
            if os.path.exists(dst):
                raise fileops.FileOpError("dst_exists", rel_path)
            fileops.safe_move(src, dst)
            try:
                st = os.stat(dst)
                self.file_index[rel_path] = (st.st_size, st.st_mtime, st.st_ctime)
            except OSError:
                pass
            oplog.log("action_restore", group=self.cfg.name, file=rel_path, md5=md5,
                      repo_name=repo_name, repo_kind=kind)
            self._after_action()

    def act_move(self, rel_path, md5, target_rel_path):
        with self.lock:
            src = self.validate_library_file(rel_path, md5)
            dst = self.abs_lib(target_rel_path)
            if os.path.exists(dst):
                raise fileops.FileOpError("dst_exists", target_rel_path)
            fileops.safe_move(src, dst)
            self.file_index.pop(rel_path, None)
            try:
                st = os.stat(dst)
                self.file_index[target_rel_path] = (st.st_size, st.st_mtime, st.st_ctime)
            except OSError:
                pass
            oplog.log("action_move", group=self.cfg.name, src=rel_path, md5=md5,
                      dst=target_rel_path)
            self._after_action()

    def act_ignore(self, md5s):
        with self.lock:
            s = frozenset(md5s)
            if not s:
                raise fileops.FileOpError("bad_request", "empty md5 set")
            self.ignored_sets.add(s)
            oplog.log("action_ignore", group=self.cfg.name, md5s=sorted(s))
            self.save_state()
            self.bump()

    def act_unignore(self, md5s):
        with self.lock:
            s = frozenset(md5s)
            if s not in self.ignored_sets:
                raise fileops.FileOpError("not_ignored", "group is not ignored")
            self.ignored_sets.discard(s)
            oplog.log("action_unignore", group=self.cfg.name, md5s=sorted(s))
            self.save_state()
            self.bump()

    def _after_action(self):
        self.tree_signature = None
        self._recompute(dict(self.file_index))
        self.bump()

    def _relativize(self, raw):
        root = self.cfg.library_root.rstrip(os.sep) + os.sep
        groups = []
        for g in raw:
            files = []
            for item in g:
                if item["path"].startswith(root):
                    files.append({**item, "path": self.rel(item["path"])})
            if len(files) >= 2:
                groups.append(files)
        return groups

    def _recompute(self, file_index):
        with self.lock:
            czkawka_groups = list(self.czkawka_groups)
        scanned = self.merge_groups(czkawka_groups, file_index)
        self.reconcile(scanned, file_index)
        self.evict_md5_cache(file_index)

    def scan_loop(self):
        while not self.stop_event.is_set():
            try:
                file_index = self.walk_files()
                sig = hash(tuple(sorted(file_index.items())))
                tree_changed = sig != self.tree_signature
                with self.lock:
                    self.file_index = file_index
                if not self.first_scan_done.is_set():
                    self.first_scan_done.set()
                    self.czkawka_wakeup.set()
                if tree_changed:
                    self.tree_signature = sig
                    self.last_tree_change = time.time()
                    self._recompute(file_index)
                    self.czkawka_wakeup.set()
            except Exception as e:
                oplog.error("scan_loop_error", group=self.cfg.name, error=str(e))
            self.stop_event.wait(self.cfg.scan_interval)

    def czkawka_loop(self):
        last_full_started = 0.0
        while not self.stop_event.is_set():
            self.czkawka_wakeup.clear()
            if not self.first_scan_done.is_set():
                self.czkawka_wakeup.wait(1.0)
                continue
            try:
                now = time.time()
                with self.lock:
                    file_index = dict(self.file_index)
                    last_tree_change = self.last_tree_change
                run_full = (now - last_full_started >= self.cfg.czkawka_full_interval
                            or last_tree_change > last_full_started)
                if run_full:
                    last_full_started = time.time()
                    raw = czkawka.run_image_scan(self.app_cfg, self.cfg)
                    if raw is not None:
                        groups = self._relativize(raw)
                        with self.lock:
                            self.czkawka_groups = groups
                        self._recompute(file_index)
                        oplog.log("czkawka_full_done", group=self.cfg.name,
                                  groups=len(groups))
            except Exception as e:
                oplog.error("czkawka_loop_error", group=self.cfg.name, error=str(e))
            timeout = max(5.0, self.cfg.czkawka_full_interval - (time.time() - last_full_started))
            self.czkawka_wakeup.wait(min(timeout, 30.0))

    def start(self):
        threading.Thread(target=self.scan_loop, daemon=True, name=f"scan-{self.cfg.name}").start()
        threading.Thread(target=self.czkawka_loop, daemon=True, name=f"czkawka-{self.cfg.name}").start()

    def stop(self):
        self.stop_event.set()
        self.czkawka_wakeup.set()
