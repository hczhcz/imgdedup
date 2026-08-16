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
        self.next_id = 1
        self.file_index = {}
        self.md5_cache = {}
        self.czkawka_groups = []
        self.tree_signature = None
        self.czkawka_seen = set()
        self.pending_new = set()
        self.state_path = os.path.join(app_cfg.state_dir, f"{group_cfg.name}.json")
        self.stop_event = threading.Event()
        self.czkawka_wakeup = threading.Event()
        self.load_state()

    def load_state(self):
        if not os.path.isfile(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.next_id = data.get("next_id", 1)
            self.groups = {}
            for gid, g in data.get("groups", {}).items():
                self.groups[int(gid)] = g
            oplog.log("state_loaded", group=self.cfg.name, groups=len(self.groups))
        except Exception as e:
            oplog.error("state_load_failed", group=self.cfg.name, error=str(e))

    def save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with self.lock:
            data = {"next_id": self.next_id, "groups": self.groups}
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

    def meta_repo_abs(self, meta):
        if not meta.get("repo_path"):
            return None
        return self.abs_repo(meta["repo_path"], meta.get("repo_kind", "fuzzy"))

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
                result[self.rel(ap)] = (st.st_size, st.st_mtime)
        return result

    def get_md5(self, rp, file_index):
        if rp not in file_index:
            return None
        size, mtime = file_index[rp]
        key = (rp, size, mtime)
        md5 = self.md5_cache.get(key)
        if md5 is None:
            try:
                md5 = file_md5(self.abs_lib(rp))
            except OSError:
                return None
            self.md5_cache[key] = md5
        return md5

    def compute_md5_groups(self, file_index):
        by_size = {}
        for rp, (size, mtime) in file_index.items():
            by_size.setdefault(size, []).append(rp)
        groups = []
        for size, paths in by_size.items():
            if len(paths) < 2:
                continue
            by_md5 = {}
            for rp in paths:
                md5 = self.get_md5(rp, file_index)
                if md5 is not None:
                    by_md5.setdefault(md5, []).append(rp)
            for md5, group in by_md5.items():
                if len(group) >= 2:
                    groups.append(sorted(group))
        stale = [k for k in self.md5_cache if k[0] not in file_index or file_index[k[0]] != (k[1], k[2])]
        for k in stale:
            del self.md5_cache[k]
        return groups

    def merge_groups(self, md5_groups, czkawka_groups, file_index):
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

        exact_pairs = set()
        similarity_of = {}
        for g in md5_groups:
            for rp in g[1:]:
                union(g[0], rp)
                exact_pairs.add(rp)
                exact_pairs.add(g[0])
        for g in czkawka_groups:
            base = g[0]["path"]
            if base not in file_index:
                continue
            for item in g[1:]:
                rp = item["path"]
                if rp not in file_index:
                    continue
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
            if members & exact_pairs and len(members & exact_pairs) >= 2:
                level = "Exact"
            else:
                sims = [similarity_of[m] for m in members if m in similarity_of]
                best = min(sims) if sims else None
                level = czkawka.classify_similarity(best, self.cfg.czkawka_hash_size) if best is not None else None
            if level is None:
                continue
            if len(members & exact_pairs) < 2 and LEVELS.index(level) < self.cfg.min_level_index():
                continue
            scanned.append({"members": sorted(members), "level": level})
        return scanned

    def reconcile(self, scanned, file_index):
        with self.lock:
            changed = False
            member_to_gid = {}
            for gid, g in self.groups.items():
                for rp in g["files"]:
                    member_to_gid.setdefault(rp, gid)

            for sg in scanned:
                gids = sorted({member_to_gid[m] for m in sg["members"] if m in member_to_gid})
                if not gids:
                    gid = self.next_id
                    self.next_id += 1
                    self.groups[gid] = {
                        "id": gid,
                        "files": {m: {"repo_path": None} for m in sg["members"]},
                        "level": sg["level"],
                        "created_at": time.time(),
                        "resolved": False,
                    }
                    for m in sg["members"]:
                        member_to_gid[m] = gid
                    changed = True
                    oplog.log("dup_group_new", group=self.cfg.name, gid=gid,
                              level=sg["level"], files=sg["members"])
                else:
                    gid = gids[0]
                    g = self.groups[gid]
                    for other in gids[1:]:
                        og = self.groups.pop(other)
                        for rp, meta in og["files"].items():
                            if rp not in g["files"]:
                                g["files"][rp] = meta
                            elif meta.get("repo_path") and not g["files"][rp].get("repo_path"):
                                g["files"][rp] = meta
                            member_to_gid[rp] = gid
                        if g.get("ignored") or og.get("ignored"):
                            g["ignored"] = False
                            oplog.log("dup_group_unignored", group=self.cfg.name, gid=gid)
                        changed = True
                        oplog.log("dup_group_merged", group=self.cfg.name, into=gid, merged=other)
                    for m in sg["members"]:
                        if m not in g["files"]:
                            g["files"][m] = {"repo_path": None}
                            member_to_gid[m] = gid
                            changed = True
                            if g.get("ignored"):
                                g["ignored"] = False
                                oplog.log("dup_group_unignored", group=self.cfg.name, gid=gid)
                            oplog.log("dup_group_extended", group=self.cfg.name, gid=gid, file=m)
                    if LEVELS.index(sg["level"]) > LEVELS.index(g["level"]):
                        g["level"] = sg["level"]
                        changed = True
                    if g["resolved"]:
                        present = [rp for rp in g["files"]
                                   if rp in file_index and not g["files"][rp].get("repo_path")]
                        if len(present) > 1:
                            g["resolved"] = False
                            changed = True

            for gid, g in list(self.groups.items()):
                present = [rp for rp in g["files"]
                           if rp in file_index and not g["files"][rp].get("repo_path")]
                in_repo = [rp for rp in g["files"] if g["files"][rp].get("repo_path")]
                resolved = len(present) <= 1
                if not in_repo and len(present) < 2:
                    del self.groups[gid]
                    changed = True
                    oplog.log("dup_group_dropped", group=self.cfg.name, gid=gid)
                elif resolved != g["resolved"]:
                    g["resolved"] = resolved
                    changed = True
                    oplog.log("dup_group_resolved" if resolved else "dup_group_reopened",
                              group=self.cfg.name, gid=gid)
            if changed:
                self.save_state()
                self.bump()
            return changed

    def refresh_statuses(self, file_index):
        with self.lock:
            changed = False
            for g in self.groups.values():
                for rp, meta in g["files"].items():
                    status = self.file_status(rp, meta, file_index)
                    if meta.get("_status") != status:
                        meta["_status"] = status
                        changed = True
            if changed:
                self.bump()

    def file_status(self, rp, meta, file_index):
        repo_path = meta.get("repo_path")
        in_lib = rp in file_index
        if repo_path:
            repo_exists = os.path.isfile(self.meta_repo_abs(meta))
            if in_lib:
                return "replaced" if repo_exists else "restored_external"
            return "in_repo" if repo_exists else "missing"
        if in_lib:
            return "present"
        return "missing"

    def sorted_files(self, g):
        return sorted(g["files"].keys())

    def group_snapshot(self, gid):
        with self.lock:
            g = self.groups.get(gid)
            if not g:
                return None
            files = []
            order = self.sorted_files(g)
            last = order[-1] if order else None
            dup_dirs = {}
            for other in self.groups.values():
                if other.get("ignored"):
                    continue
                for rp in other["files"]:
                    dup_dirs.setdefault(os.path.dirname(rp), {})[os.path.basename(rp)] = other["id"]
            for rp in order:
                meta = g["files"][rp]
                status = meta.get("_status") or self.file_status(rp, meta, self.file_index)
                info = {
                    "rel_path": rp,
                    "repo_path": meta.get("repo_path"),
                    "repo_kind": meta.get("repo_kind", "fuzzy"),
                    "status": status,
                    "is_last": rp == last,
                    "size": None, "mtime": None, "width": None, "height": None,
                    "neighbors_prev": [], "neighbors_next": [],
                }
                ap = self.abs_lib(rp) if status in ("present", "replaced", "restored_external") else (
                    self.meta_repo_abs(meta) if meta.get("repo_path") and status == "in_repo" else None)
                if ap and os.path.isfile(ap):
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
                    idx = None
                    for i, s in enumerate(siblings):
                        if s > base:
                            idx = i
                            break
                    if idx is None:
                        idx = len(siblings)
                    siblings = siblings[:idx] + [None] + siblings[idx:]
                    idx = siblings.index(None)
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
            present = [f for f in files if f["status"] in ("present", "replaced", "restored_external")]
            gaps_ok = True
            if self.cfg.keep_no_gap:
                for f in files:
                    if f["status"] == "in_repo" and not f["is_last"]:
                        gaps_ok = False
            can_complete = len(present) == 1 and gaps_ok
            return {
                "id": gid,
                "level": g["level"],
                "resolved": g["resolved"],
                "ignored": bool(g.get("ignored")),
                "can_ignore": self.group_all_same_md5(g),
                "keep_no_gap": self.cfg.keep_no_gap,
                "can_complete": can_complete,
                "gaps_ok": gaps_ok,
                "files": files,
            }

    def _dir_siblings(self, rel_dir):
        try:
            abs_dir = self.abs_lib(rel_dir) if rel_dir else self.cfg.library_root
            names = [n for n in os.listdir(abs_dir)
                     if is_image(n) and os.path.isfile(os.path.join(abs_dir, n))
                     and not self.excluded(os.path.join(abs_dir, n))]
            return sorted(names)
        except OSError:
            return []

    def list_snapshot(self):
        with self.lock:
            items = []
            for gid in sorted(self.groups.keys()):
                g = self.groups[gid]
                if g.get("ignored"):
                    continue
                files = []
                for rp in self.sorted_files(g):
                    meta = g["files"][rp]
                    size = None
                    if rp in self.file_index:
                        size = self.file_index[rp][0]
                    elif meta.get("repo_path"):
                        try:
                            size = os.path.getsize(self.meta_repo_abs(meta))
                        except OSError:
                            pass
                    files.append({
                        "rel_path": rp,
                        "name": os.path.basename(rp),
                        "size": size,
                        "status": meta.get("_status", "present"),
                    })
                items.append({
                    "id": gid,
                    "level": g["level"],
                    "resolved": g["resolved"],
                    "created_at": g["created_at"],
                    "files": files,
                })
            return {"version": self.version, "groups": items}

    def _pick_repo_kind(self, g, rel_path):
        my_md5 = self.get_md5(rel_path, self.file_index)
        if my_md5 is None:
            return "fuzzy"
        for rp, meta in g["files"].items():
            if rp == rel_path or meta.get("repo_path"):
                continue
            if self.get_md5(rp, self.file_index) == my_md5:
                return "exact"
        return "fuzzy"

    def act_move_to_repo(self, gid, rel_path, repo_name=None):
        with self.lock:
            g = self.groups.get(gid)
            if not g or rel_path not in g["files"]:
                raise fileops.FileOpError("not_found", "group or file not found")
            meta = g["files"][rel_path]
            if meta.get("repo_path"):
                raise fileops.FileOpError("already_in_repo", "file already in repo")
            name = repo_name or os.path.basename(rel_path)
            if os.sep in name or name in ("", ".", ".."):
                raise fileops.FileOpError("bad_name", f"invalid repo name: {name}")
            kind = self._pick_repo_kind(g, rel_path)
            src = self.abs_lib(rel_path)
            dst = self.abs_repo(name, kind)
            if os.path.exists(dst):
                raise fileops.FileOpError("dst_exists", name, {"repo_kind": kind})
            fileops.safe_move(src, dst)
            meta["repo_path"] = name
            meta["repo_kind"] = kind
            self.file_index.pop(rel_path, None)
            oplog.log("action_move_to_repo", group=self.cfg.name, gid=gid,
                      file=rel_path, repo_name=name, repo_kind=kind)
            self._after_action()

    def act_restore(self, gid, rel_path):
        with self.lock:
            g = self.groups.get(gid)
            if not g or rel_path not in g["files"]:
                raise fileops.FileOpError("not_found", "group or file not found")
            meta = g["files"][rel_path]
            name = meta.get("repo_path")
            if not name:
                raise fileops.FileOpError("not_in_repo", "file not in repo")
            src = self.meta_repo_abs(meta)
            dst = self.abs_lib(rel_path)
            if os.path.exists(dst):
                raise fileops.FileOpError("dst_exists", rel_path)
            fileops.safe_move(src, dst)
            meta["repo_path"] = None
            meta["repo_kind"] = None
            try:
                st = os.stat(dst)
                self.file_index[rel_path] = (st.st_size, st.st_mtime)
            except OSError:
                pass
            oplog.log("action_restore", group=self.cfg.name, gid=gid, file=rel_path)
            self._after_action()

    def act_relocate(self, gid, rel_path, target_rel_path):
        with self.lock:
            g = self.groups.get(gid)
            if not g or rel_path not in g["files"] or target_rel_path not in g["files"]:
                raise fileops.FileOpError("not_found", "group or file not found")
            meta = g["files"][rel_path]
            target = g["files"][target_rel_path]
            if meta.get("repo_path"):
                raise fileops.FileOpError("in_repo", "source file is in repo")
            if not target.get("repo_path"):
                raise fileops.FileOpError("target_not_vacated", "target slot not vacated")
            src = self.abs_lib(rel_path)
            dst = self.abs_lib(target_rel_path)
            if os.path.exists(dst):
                raise fileops.FileOpError("dst_exists", target_rel_path)
            fileops.safe_move(src, dst)
            self.file_index.pop(rel_path, None)
            try:
                st = os.stat(dst)
                self.file_index[target_rel_path] = (st.st_size, st.st_mtime)
            except OSError:
                pass
            oplog.log("action_relocate", group=self.cfg.name, gid=gid,
                      src=rel_path, dst=target_rel_path)
            self._after_action()

    def group_all_same_md5(self, g):
        md5s = []
        for rp, meta in g["files"].items():
            if meta.get("repo_path"):
                return False
            md5 = self.get_md5(rp, self.file_index)
            if md5 is None:
                return False
            md5s.append(md5)
        return len(md5s) >= 2 and len(set(md5s)) == 1

    def act_ignore(self, gid):
        with self.lock:
            g = self.groups.get(gid)
            if not g:
                raise fileops.FileOpError("not_found", "group not found")
            if not self.group_all_same_md5(g):
                raise fileops.FileOpError(
                    "not_all_exact",
                    "group can only be ignored when all files have identical md5")
            g["ignored"] = True
            oplog.log("action_ignore", group=self.cfg.name, gid=gid,
                      files=self.sorted_files(g))
            self.save_state()
            self.bump()

    def act_unignore(self, gid):
        with self.lock:
            g = self.groups.get(gid)
            if not g:
                raise fileops.FileOpError("not_found", "group not found")
            g["ignored"] = False
            oplog.log("action_unignore", group=self.cfg.name, gid=gid)
            self.save_state()
            self.bump()

    def act_complete(self, gid):
        with self.lock:
            g = self.groups.get(gid)
            if not g:
                raise fileops.FileOpError("not_found", "group not found")
            g["done"] = True
            oplog.log("action_complete", group=self.cfg.name, gid=gid)
            self.save_state()
            self.bump()

    def _after_action(self):
        self.tree_signature = None
        self.refresh_statuses(self.file_index)
        scanned = self.merge_groups(self.compute_md5_groups(dict(self.file_index)),
                                    self.czkawka_groups, self.file_index)
        self.reconcile(scanned, self.file_index)
        self.save_state()
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
        md5_groups = self.compute_md5_groups(file_index)
        scanned = self.merge_groups(md5_groups, self.czkawka_groups, file_index)
        self.reconcile(scanned, file_index)
        self.refresh_statuses(file_index)

    def scan_loop(self):
        while not self.stop_event.is_set():
            try:
                file_index = self.walk_files()
                sig = hash(tuple(sorted(file_index.items())))
                tree_changed = sig != self.tree_signature
                with self.lock:
                    prev_keys = set(self.czkawka_seen)
                    self.file_index = file_index
                if tree_changed:
                    self.tree_signature = sig
                    new_keys = {(rp, size, mtime) for rp, (size, mtime) in file_index.items()
                                if (rp, size, mtime) not in prev_keys}
                    if new_keys:
                        with self.lock:
                            self.pending_new |= new_keys
                        self.czkawka_wakeup.set()
                    self._recompute(file_index)
                else:
                    self.refresh_statuses(file_index)
            except Exception as e:
                oplog.error("scan_loop_error", group=self.cfg.name, error=str(e))
            self.stop_event.wait(self.cfg.scan_interval)

    def _merge_czkawka_results(self, new_groups):
        with self.lock:
            merged = {tuple(sorted(x["path"] for x in g)): g for g in self.czkawka_groups}
            for g in new_groups:
                merged[tuple(sorted(x["path"] for x in g))] = g
            self.czkawka_groups = list(merged.values())

    def czkawka_loop(self):
        last_full = 0.0
        while not self.stop_event.is_set():
            self.czkawka_wakeup.clear()
            try:
                now = time.time()
                with self.lock:
                    pending = set(self.pending_new)
                    file_index = dict(self.file_index)
                run_full = now - last_full >= self.cfg.czkawka_full_interval
                if run_full:
                    raw = czkawka.run_image_scan(self.app_cfg, self.cfg)
                    if raw is not None:
                        last_full = time.time()
                        groups = self._relativize(raw)
                        with self.lock:
                            self.czkawka_groups = groups
                            self.czkawka_seen = {(rp, s, m) for rp, (s, m) in file_index.items()}
                            self.pending_new -= pending
                        self._recompute(file_index)
                        oplog.log("czkawka_full_done", group=self.cfg.name,
                                  groups=len(groups))
                elif pending:
                    new_abs = []
                    valid_keys = set()
                    for key in pending:
                        rp, size, mtime = key
                        if file_index.get(rp) == (size, mtime):
                            new_abs.append(self.abs_lib(rp))
                            valid_keys.add(key)
                    if new_abs:
                        raw = czkawka.run_incremental_scan(self.app_cfg, self.cfg, new_abs)
                        if raw is not None:
                            groups = self._relativize(raw)
                            self._merge_czkawka_results(groups)
                            with self.lock:
                                self.czkawka_seen |= valid_keys
                                self.pending_new -= pending
                            self._recompute(file_index)
                            oplog.log("czkawka_incremental_done", group=self.cfg.name,
                                      new_files=len(new_abs), groups=len(groups))
                    else:
                        with self.lock:
                            self.pending_new -= pending
            except Exception as e:
                oplog.error("czkawka_loop_error", group=self.cfg.name, error=str(e))
            with self.lock:
                has_pending = bool(self.pending_new)
            timeout = 2.0 if has_pending else max(
                5.0, self.cfg.czkawka_full_interval - (time.time() - last_full))
            self.czkawka_wakeup.wait(min(timeout, 30.0))

    def start(self):
        threading.Thread(target=self.scan_loop, daemon=True, name=f"scan-{self.cfg.name}").start()
        threading.Thread(target=self.czkawka_loop, daemon=True, name=f"czkawka-{self.cfg.name}").start()

    def stop(self):
        self.stop_event.set()
        self.czkawka_wakeup.set()
