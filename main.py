import sys

from imgdedup import oplog
from imgdedup.config import load_config, DEFAULT_CONFIG_PATH
from imgdedup.scanner import GroupRuntime
from imgdedup.server import run_server


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    cfg = load_config(config_path)
    oplog.init(cfg.log_file)
    oplog.log("startup", config=config_path, groups=[g.name for g in cfg.groups])
    runtimes = {}
    for g in cfg.groups:
        rt = GroupRuntime(cfg, g)
        rt.start()
        runtimes[g.name] = rt
    try:
        run_server(cfg, runtimes)
    except KeyboardInterrupt:
        for rt in runtimes.values():
            rt.stop()
        oplog.log("shutdown")


if __name__ == "__main__":
    main()
