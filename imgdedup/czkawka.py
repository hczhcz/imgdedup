import json
import os
import shutil
import subprocess
import tempfile

from . import oplog

SIMILAR_VALUES = {
    8: [1, 2, 5, 7, 14, 20],
    16: [2, 5, 15, 30, 40, 40],
    32: [4, 10, 20, 40, 80, 80],
    64: [6, 20, 40, 80, 160, 160],
}

LEVEL_ORDER = ["VeryHigh", "High", "Medium", "Small", "VerySmall", "Minimal"]


def classify_similarity(similarity, hash_size):
    thresholds = SIMILAR_VALUES[hash_size]
    for level, t in zip(LEVEL_ORDER, thresholds):
        if similarity <= t:
            return level
    return None


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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            oplog.error("czkawka_failed", group=group.name, mode=label,
                        code=proc.returncode, stderr=proc.stderr[-2000:])
            return None
        with open(out_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        oplog.error("czkawka_error", group=group.name, mode=label, error=str(e))
        return None
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


def _item(raw):
    return {
        "path": raw["path"],
        "size": raw["size"],
        "width": raw["width"],
        "height": raw["height"],
        "similarity": raw["similarity"],
    }


def run_image_scan(cfg, group):
    fd, out_path = tempfile.mkstemp(suffix=".json", prefix="imgdedup-czkawka-")
    os.close(fd)
    cmd = _base_cmd(cfg, group, out_path)
    cmd += ["-d", group.library_root]
    raw = _run(cfg, group, cmd, out_path, "full")
    if raw is None:
        return None
    result = []
    for g in raw:
        files = [_item(item) for item in g]
        if len(files) >= 2:
            result.append(files)
    return result


def run_incremental_scan(cfg, group, new_abs_paths):
    warm_dir = tempfile.mkdtemp(prefix="imgdedup-warm-")
    mapping = {}
    try:
        for i, ap in enumerate(new_abs_paths):
            ext = os.path.splitext(ap)[1]
            warm_name = f"{i:06d}{ext}"
            warm_path = os.path.join(warm_dir, warm_name)
            try:
                shutil.copy2(ap, warm_path)
                mapping[warm_path] = ap
            except OSError:
                continue
        if not mapping:
            return []
        result = []
        fd, out_path = tempfile.mkstemp(suffix=".json", prefix="imgdedup-czkawka-")
        os.close(fd)
        cmd = _base_cmd(cfg, group, out_path)
        cmd += ["-d", warm_dir, "-r", group.library_root]
        for ap in mapping.values():
            cmd += ["-E", "*" + ap]
        raw = _run(cfg, group, cmd, out_path, "incremental")
        if raw is None:
            return None
        for g in raw:
            ref, others = g[0], g[1]
            files = [_item(ref)]
            for item in others:
                real = mapping.get(item["path"])
                if real is None or real == ref["path"]:
                    continue
                it = _item(item)
                it["path"] = real
                files.append(it)
            if len(files) >= 2:
                result.append(files)
        if len(mapping) >= 2:
            fd, out_path = tempfile.mkstemp(suffix=".json", prefix="imgdedup-czkawka-")
            os.close(fd)
            cmd = _base_cmd(cfg, group, out_path)
            cmd += ["-d", warm_dir]
            raw = _run(cfg, group, cmd, out_path, "incremental-inner")
            if raw is not None:
                for g in raw:
                    files = []
                    for item in g:
                        real = mapping.get(item["path"])
                        if real is None:
                            continue
                        it = _item(item)
                        it["path"] = real
                        files.append(it)
                    if len(files) >= 2:
                        result.append(files)
        return result
    finally:
        shutil.rmtree(warm_dir, ignore_errors=True)
