import json
import os
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


def _run(cfg, group, cmd, out_path, label):
    try:
        os.remove(out_path)
    except OSError:
        pass
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except Exception as e:
        oplog.error("czkawka_error", group=group.name, mode=label, error=str(e))
        return None
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
