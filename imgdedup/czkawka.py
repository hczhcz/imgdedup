import json
import os
import struct
import subprocess

from . import oplog

SIMILAR_VALUES = {
    8: [0, 1, 2, 5, 7, 14, 20],
    16: [0, 2, 5, 15, 30, 40, 40],
    32: [0, 4, 10, 20, 40, 80, 80],
    64: [0, 6, 20, 40, 80, 160, 160],
}

LEVEL_ORDER = ["Original", "VeryHigh", "High", "Medium", "Small", "VerySmall", "Minimal"]


def classify_similarity(similarity, hash_size):
    thresholds = SIMILAR_VALUES[hash_size]
    for level, t in zip(LEVEL_ORDER, thresholds):
        if similarity <= t:
            return level
    return None


def _safe_name(name):
    out = name.replace(os.sep, "_")
    if os.altsep:
        out = out.replace(os.altsep, "_")
    return out


def _work_dir(cfg, group):
    d = os.path.join(cfg.state_dir, "czkawka", _safe_name(group.name))
    os.makedirs(d, exist_ok=True)
    return d


def _out_path(cfg, group, name):
    return os.path.join(_work_dir(cfg, group), f"{name}.json")


def _base_cmd(cfg, group, out_path):
    cmd = [
        cfg.czkawka_cli, "image",
        "-m", str(group.min_file_size),
        "-s", group.czkawka_similarity_preset,
        "-g", group.czkawka_hash_alg,
        "-z", group.czkawka_image_filter,
        "-c", str(group.czkawka_hash_size),
        "-p", out_path,
        "-N", "-M", "-W",
    ]
    for pat in group.exclude_patterns:
        cmd += ["-E", pat]
    lib_prefix = group.library_root.rstrip(os.sep) + os.sep
    for repo in (group.dup_repo, group.exact_dup_repo):
        if repo.startswith(lib_prefix):
            cmd += ["-e", repo]
    return cmd


def _cache_dir(cfg, group):
    d = os.path.join(_work_dir(cfg, group), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_file_name(group):
    return (f"cache_similar_images_{group.czkawka_hash_size}_"
            f"{group.czkawka_hash_alg}_{group.czkawka_image_filter}_90_fast_resize.bin")


def _parse_cache_bin(path):
    entries = {}
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return entries
    try:
        off = 8
        (count,) = struct.unpack_from("<Q", data, 0)
        for _ in range(count):
            start = off
            (plen,) = struct.unpack_from("<Q", data, off)
            fpath = data[off + 8:off + 8 + plen]
            off += 8 + plen + 24
            (hlen,) = struct.unpack_from("<Q", data, off)
            off += 8 + hlen + 4
            entries[fpath] = data[start:off]
        if off != len(data):
            return {}
    except (struct.error, IndexError):
        return {}
    return entries


def _write_cache_bin(path, entries):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(struct.pack("<Q", len(entries)))
        for raw in entries.values():
            f.write(raw)
    os.replace(tmp, path)


def _seed_cache(cfg, group):
    d = _cache_dir(cfg, group)
    name = _cache_file_name(group)
    master = os.path.join(d, "master_" + name)
    active = os.path.join(d, name)
    entries = _parse_cache_bin(master)
    if entries:
        _write_cache_bin(active, entries)
    return d, master, active, entries


def _merge_cache(master, active, master_entries):
    new_entries = _parse_cache_bin(active)
    if not new_entries and not master_entries:
        return
    before = len(master_entries)
    master_entries.update(new_entries)
    if len(master_entries) != before or new_entries:
        _write_cache_bin(master, master_entries)


def _run(cfg, group, cmd, out_path, label):
    try:
        os.remove(out_path)
    except OSError:
        pass
    cache_dir, master, active, master_entries = _seed_cache(cfg, group)
    env = dict(os.environ, CZKAWKA_CACHE_PATH=cache_dir)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, env=env)
    except Exception as e:
        oplog.error("czkawka_error", group=group.name, mode=label, error=str(e))
        return None
    _merge_cache(master, active, master_entries)
    if proc.returncode != 0:
        oplog.error("czkawka_failed", group=group.name, mode=label,
                    code=proc.returncode, stderr=proc.stderr[-2000:])
        return None
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        oplog.error("czkawka_error", group=group.name, mode=label, error=str(e))
        return None


def _item(raw):
    return {
        "path": raw["path"],
        "size": raw["size"],
        "mtime": raw["modified_date"],
        "width": raw["width"],
        "height": raw["height"],
        "similarity": raw["similarity"],
    }


def _parse_groups(raw):
    result = []
    for g in raw:
        files = []
        for entry in g:
            for item in (entry if isinstance(entry, list) else [entry]):
                files.append(_item(item))
        if len(files) >= 2:
            result.append(files)
    return result


def run_image_scan(cfg, group):
    out_path = _out_path(cfg, group, "full")
    cmd = _base_cmd(cfg, group, out_path)
    cmd += ["-d", group.library_root]
    raw = _run(cfg, group, cmd, out_path, "full")
    return None if raw is None else _parse_groups(raw)


def run_incremental_scan(cfg, group, main_dirs, ref_dirs):
    result = []
    if not main_dirs:
        return result
    if ref_dirs:
        out_path = _out_path(cfg, group, "incr")
        cmd = _base_cmd(cfg, group, out_path)
        for d in main_dirs:
            cmd += ["-d", d]
        for r in ref_dirs:
            cmd += ["-r", r]
        raw = _run(cfg, group, cmd, out_path, "incremental-ref")
        if raw is None:
            return None
        result += _parse_groups(raw)
    inner_out = _out_path(cfg, group, "inner")
    cmd = _base_cmd(cfg, group, inner_out)
    for d in main_dirs:
        cmd += ["-d", d]
    raw = _run(cfg, group, cmd, inner_out, "incremental-inner")
    if raw is None:
        return None
    result += _parse_groups(raw)
    return result
