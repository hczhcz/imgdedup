import json
import os
from dataclasses import dataclass, field

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

LEVELS = ["Minimal", "VerySmall", "Small", "Medium", "High", "VeryHigh", "Exact"]


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
    scan_interval: float = 5.0
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


def load_config(path=DEFAULT_CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    base_dir = os.path.dirname(os.path.abspath(path))
    groups = []
    for g in raw.get("groups", []):
        gc = GroupConfig(
            name=g["name"],
            library_root=os.path.abspath(g["library_root"]),
            exclude_patterns=g.get("exclude_patterns", []),
            dup_repo=os.path.abspath(g["dup_repo"]),
            exact_dup_repo=os.path.abspath(g.get("exact_dup_repo") or g["dup_repo"]),
            czkawka_hash_size=g.get("czkawka_hash_size", 16),
            czkawka_hash_alg=g.get("czkawka_hash_alg", "Gradient"),
            czkawka_image_filter=g.get("czkawka_image_filter", "Nearest"),
            czkawka_similarity_preset=g.get("czkawka_similarity_preset", "Minimal"),
            min_file_size=g.get("min_file_size", 1024),
            min_level=g.get("min_level", "Minimal"),
            keep_no_gap=g.get("keep_no_gap", False),
            scan_interval=g.get("scan_interval", 5.0),
            czkawka_full_interval=g.get("czkawka_full_interval", 600.0),
        )
        if gc.min_level not in LEVELS:
            raise ValueError(f"invalid min_level: {gc.min_level}")
        groups.append(gc)
    state_dir = raw.get("state_dir") or os.path.join(base_dir, "state")
    log_file = raw.get("log_file") or os.path.join(base_dir, "imgdedup.log")
    return AppConfig(
        port=raw.get("port", 19810),
        czkawka_cli=raw.get("czkawka_cli", "czkawka_cli"),
        state_dir=os.path.abspath(state_dir),
        log_file=os.path.abspath(log_file),
        groups=groups,
    )
