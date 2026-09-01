import datetime
import json
import os
from dataclasses import dataclass, field

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

LEVELS = ["Minimal", "VerySmall", "Small", "Medium", "High", "VeryHigh", "Original", "Exact"]


@dataclass
class GroupConfig:
    name: str
    library_root: str
    exclude_patterns: list = field(default_factory=list)
    dup_repo: str = ""
    exact_dup_repo: str = ""
    czkawka_hash_size: int = 16
    czkawka_hash_alg: str = "Gradient"
    czkawka_image_filter: str = "Nearest"
    czkawka_similarity_preset: str = "Minimal"
    min_file_size: int = 1024
    min_level: str = "Minimal"
    keep_no_gap: bool = False
    hide_before: float = None
    czkawka_full_interval: float = 600.0

    def min_level_index(self):
        return LEVELS.index(self.min_level)


@dataclass
class AppConfig:
    port: int = 19810
    czkawka_cli: str = "czkawka_cli"
    state_dir: str = ""
    log_file: str = ""
    groups: list = field(default_factory=list)


def parse_hide_before(value):
    if value is None:
        return None
    return datetime.datetime.fromisoformat(value).timestamp()


def load_config(path=DEFAULT_CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    base_dir = os.path.dirname(os.path.abspath(path))
    groups = []
    optional = ["exclude_patterns", "czkawka_hash_size", "czkawka_hash_alg",
                "czkawka_image_filter", "czkawka_similarity_preset", "min_file_size",
                "min_level", "keep_no_gap", "czkawka_full_interval"]
    for g in raw.get("groups", []):
        gc = GroupConfig(
            name=g["name"],
            library_root=os.path.abspath(g["library_root"]),
            dup_repo=os.path.abspath(g["dup_repo"]),
            exact_dup_repo=os.path.abspath(g.get("exact_dup_repo") or g["dup_repo"]),
            hide_before=parse_hide_before(g.get("hide_before")),
            **{k: g[k] for k in optional if k in g},
        )
        if gc.min_level not in LEVELS:
            raise ValueError(f"invalid min_level: {gc.min_level}")
        if gc.czkawka_hash_size not in (8, 16, 32, 64):
            raise ValueError(f"invalid czkawka_hash_size: {gc.czkawka_hash_size}")
        if not os.path.isdir(gc.library_root):
            raise ValueError(f"library_root not found: {gc.library_root}")
        groups.append(gc)
    names = [g.name for g in groups]
    if len(set(names)) != len(names):
        raise ValueError("duplicate group names")
    state_dir = raw.get("state_dir") or os.path.join(base_dir, "state")
    log_file = raw.get("log_file") or os.path.join(base_dir, "imgdedup.log")
    return AppConfig(
        port=raw.get("port", 19810),
        czkawka_cli=raw.get("czkawka_cli", "czkawka_cli"),
        state_dir=os.path.abspath(state_dir),
        log_file=os.path.abspath(log_file),
        groups=groups,
    )
