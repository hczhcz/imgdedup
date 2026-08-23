import bisect
import fnmatch
import hashlib
import re
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
        self.groups = {}
        self.file_index = {}
        self.md5_cache = {}
        self.czkawka_groups = []
        self.changed_dirs = set()
        self.state_version = 0
        self._list_cache = None
        self._exclude_res = [re.compile(fnmatch.translate(p)) for p in group_cfg.exclude_patterns]
        self.action_seq = 0
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

    def rel(self, abs_path):
        return os.path.relpath(abs_path, self.cfg.library_root)

    def file_dir(self, rel_path):
        return os.path.dirname(rel_path)

    def _abs_under(self, root, rel_path, what):
        p = os.path.abspath(os.path.join(root, rel_path))
        if not self._is_under(p, root.rstrip(os.sep)):
            raise fileops.FileOpError("bad_path", f"path escapes {what}: {rel_path}")
        return p

    def abs_lib(self, rel_path):
        return self._abs_under(self.cfg.library_root, rel_path, "library")

    def abs_repo(self, repo_name, kind="fuzzy"):
        root = self.cfg.exact_dup_repo if kind == "exact" else self.cfg.dup_repo
        return self._abs_under(root, repo_name, "repo")

    def excluded(self, abs_path):
        for rx in self._exclude_res:
            if rx.match(abs_path):
                return True
        return False

    def walk_files(self):
        result = {}
        root = os.path.abspath(self.cfg.library_root)
        repos = {self.cfg.dup_repo.rstrip(os.sep), self.cfg.exact_dup_repo.rstrip(os.sep)}
        min_size = self.cfg.min_file_size
        root_prefix_len = len(root.rstrip(os.sep)) + 1
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                entries = os.scandir(d)
            except OSError:
                continue
            with entries:
                for e in entries:
                    ap = e.path
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if ap not in repos and not self.excluded(ap):
                                stack.append(ap)
                            continue
                        if not e.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if not is_image(e.name) or self.excluded(ap):
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    if st.st_size < min_size:
                        continue
                    result[ap[root_prefix_len:]] = (st.st_size, st.st_mtime, st.st_ctime)
        return result

    def get_md5(self, rp, file_index):
        if rp not in file_index:
            return None
        return self.get_path_md5(self.abs_lib(rp))

    def evict_md5_cache(self, file_index):
        lib = {self.abs_lib(rp) for rp in file_index}
        root = self.cfg.library_root.rstrip(os.sep)
        stale = [p for p in self.md5_cache
                 if self._is_under(p, root) and p not in lib]
        for p in stale:
            del self.md5_cache[p]

    def get_path_md5(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (st.st_size, st.st_mtime)
        entry = self.md5_cache.get(path)
        if entry is not None and entry[0] == key:
            return entry[1]
        try:
            md5 = file_md5(path)
        except OSError:
            return None
        self.md5_cache[path] = (key, md5)
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
                md5s = [self.get_md5(m, file_index) for m in members]
                md5s = [m for m in md5s if m is not None]
                if len(md5s) == len(set(md5s)):
                    continue
            scanned.append({"members": sorted(members), "level": level})
        return scanned

    def reconcile(self, scanned):
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
            if groups != self.groups:
                self.groups = groups
                self._bump_version()

    def group_md5_set(self, g):
        md5s = set()
        for rp in g["files"]:
            md5 = self.get_md5(rp, self.file_index)
            if md5 is None:
                return None
            md5s.add(md5)
        return frozenset(md5s) if md5s else None

    def ignored_match(self, g):
        s = self.group_md5_set(g) if self.ignored_sets else None
        return s if s in self.ignored_sets else None

    def has_exact_pair(self, g):
        md5s = [m for m in (self.get_md5(rp, self.file_index) for rp in g["files"])
                if m is not None]
        return len(md5s) != len(set(md5s))

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
                names = [s for s in siblings if s != base]
                idx = bisect.bisect_left(names, base)

                def neighbor_infos(part):
                    return [{"rel_path": os.path.join(d, s), "dup_gid": dir_dups.get(s)}
                            for s in part]

                info["neighbors_prev"] = neighbor_infos(names[max(0, idx - 4):idx])
                info["neighbors_next"] = neighbor_infos(names[idx:idx + 4])
                files.append(info)
            level = "Exact" if any(n >= 2 for n in md5_counts.values()) else g["level"]
            return {
                "id": gid,
                "level": level,
                "ignored": self.ignored_match(g) is not None,
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

    def _bump_version(self):
        self.state_version += 1
        self._list_cache = None

    def list_snapshot(self):
        with self.lock:
            if self._list_cache is not None and self._list_cache[0] == self.state_version:
                return self._list_cache[1]
            items = []
            ignored = []
            old = []
            for gid in sorted(self.groups.keys()):
                g = self.groups[gid]
                exact = self.has_exact_pair(g)
                ignored_set = self.ignored_match(g)
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
            snap = {"groups": items, "ignored": ignored, "old": old}
            self._list_cache = (self.state_version, snap)
            return snap

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
            self._after_action([rel_path])
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
            self._after_action([rel_path])

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
            self._after_action([rel_path, target_rel_path])

    def act_ignore(self, md5s):
        with self.lock:
            s = frozenset(md5s)
            if not s:
                raise fileops.FileOpError("bad_request", "empty md5 set")
            self.ignored_sets.add(s)
            self._bump_version()
            oplog.log("action_ignore", group=self.cfg.name, md5s=sorted(s))
            self.save_state()

    def act_unignore(self, md5s):
        with self.lock:
            s = frozenset(md5s)
            if s not in self.ignored_sets:
                raise fileops.FileOpError("not_ignored", "group is not ignored")
            self.ignored_sets.discard(s)
            self._bump_version()
            oplog.log("action_unignore", group=self.cfg.name, md5s=sorted(s))
            self.save_state()

    def _after_action(self, rel_paths=()):
        with self.lock:
            self.action_seq += 1
            self._bump_version()
            self.changed_dirs |= {self.file_dir(rp) for rp in rel_paths}
        self._recompute()
        self.czkawka_wakeup.set()

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

    def _recompute(self):
        with self.lock:
            czkawka_groups = list(self.czkawka_groups)
            file_index = dict(self.file_index)
        scanned = self.merge_groups(czkawka_groups, file_index)
        self.reconcile(scanned)
        self.evict_md5_cache(file_index)

    def _merge_czkawka_results(self, new_groups):
        with self.lock:
            merged = {tuple(sorted(x["path"] for x in g)): g for g in self.czkawka_groups}
            for g in new_groups:
                merged[tuple(sorted(x["path"] for x in g))] = g
            self.czkawka_groups = list(merged.values())

    @staticmethod
    def _is_under(path, base):
        return path == base or path.startswith(base + os.sep)

    def _minimal_dirs(self, dirs):
        out = []
        for d in sorted(dirs):
            if not any(self._is_under(d, o) for o in out):
                out.append(d)
        return out

    def _incremental_dirs(self, file_index, changed_dirs):
        if "" in changed_dirs:
            return [self.cfg.library_root], []
        all_dirs = {os.path.dirname(rp) for rp in file_index}
        all_dirs.discard("")
        main = {d for d in changed_dirs if d}
        for d in list(main):
            anc = os.path.dirname(d)
            while anc:
                if anc in all_dirs:
                    main.add(anc)
                anc = os.path.dirname(anc)
        main = set(self._minimal_dirs(main))
        ref = {d for d in all_dirs
               if not any(self._is_under(d, m) or self._is_under(m, d) for m in main)}
        ref = set(self._minimal_dirs(ref))
        return [self.abs_lib(d) for d in sorted(main)], [self.abs_lib(d) for d in sorted(ref)]

    def scan_loop(self):
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    seq = self.action_seq
                file_index = self.walk_files()
                with self.lock:
                    if self.action_seq != seq:
                        continue
                    prev = self.file_index
                    if file_index != prev:
                        self.file_index = file_index
                        self._bump_version()
                if not self.first_scan_done.is_set():
                    self.first_scan_done.set()
                    self.czkawka_wakeup.set()
                added = [rp for rp, v in file_index.items() if prev.get(rp) != v]
                removed = [rp for rp in prev if rp not in file_index]
                if added or removed:
                    if added:
                        with self.lock:
                            self.changed_dirs |= {self.file_dir(rp) for rp in added}
                    self._recompute()
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
                    changed_dirs = set(self.changed_dirs)
                if now - last_full_started >= self.cfg.czkawka_full_interval:
                    last_full_started = time.time()
                    raw = czkawka.run_image_scan(self.app_cfg, self.cfg)
                    if raw is not None:
                        groups = self._relativize(raw)
                        with self.lock:
                            self.czkawka_groups = groups
                        self._recompute()
                        oplog.log("czkawka_full_done", group=self.cfg.name,
                                  groups=len(groups))
                elif changed_dirs:
                    main_dirs, ref_dirs = self._incremental_dirs(file_index, changed_dirs)
                    if main_dirs:
                        raw = czkawka.run_incremental_scan(self.app_cfg, self.cfg,
                                                           main_dirs, ref_dirs)
                        if raw is not None:
                            groups = self._relativize(raw)
                            self._merge_czkawka_results(groups)
                            with self.lock:
                                self.changed_dirs -= changed_dirs
                            self._recompute()
                            oplog.log("czkawka_incremental_done", group=self.cfg.name,
                                      dirs=len(main_dirs), groups=len(groups))
                    else:
                        with self.lock:
                            self.changed_dirs -= changed_dirs
            except Exception as e:
                oplog.error("czkawka_loop_error", group=self.cfg.name, error=str(e))
            with self.lock:
                has_changed = bool(self.changed_dirs)
            timeout = 2.0 if has_changed else max(
                5.0, self.cfg.czkawka_full_interval - (time.time() - last_full_started))
            self.czkawka_wakeup.wait(min(timeout, 30.0))

    def start(self):
        threading.Thread(target=self.scan_loop, daemon=True, name=f"scan-{self.cfg.name}").start()
        threading.Thread(target=self.czkawka_loop, daemon=True, name=f"czkawka-{self.cfg.name}").start()

    def stop(self):
        self.stop_event.set()
        self.czkawka_wakeup.set()
