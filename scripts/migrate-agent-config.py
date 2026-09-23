#!/usr/bin/env python3
"""将旧单实例配置迁入统一配置，保留原件与稳定身份。"""

import argparse
import json
import os
from pathlib import Path
import tempfile


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mesh-migration-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # 不覆盖并发创建的配置或身份文件。
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def migrate(source, target, working_directory, apply=False):
    source, target = Path(source).resolve(), Path(target).resolve()
    cfg = json.loads(source.read_text())
    if "agents" in cfg:
        raise ValueError("Source is already a shared configuration")
    if target.exists():
        raise ValueError("Target already exists; refusing to overwrite")
    state = Path(cfg.get("state_file", "./data/agent-state.json"))
    if not state.is_absolute():
        if working_directory is None:
            raise ValueError("Relative state path requires --working-directory")
        state = Path(working_directory).resolve() / state
    identity = json.loads(state.read_text())
    if not identity.get("device_id"):
        raise ValueError("Existing identity has no device_id")
    destination = target.parent.parent / "data" / "agent-state.json"
    if destination.exists() and json.loads(destination.read_text()) != identity:
        raise ValueError("Destination identity conflicts with source")
    instance = dict(cfg)
    instance.pop("state_file", None)
    shared = {key: instance.pop(key) for key in ("gateway_url", "enroll_token") if key in instance}
    shared["agents"] = {"default": instance}
    if apply:
        if not destination.exists():
            write_private(destination, identity)
        write_private(target, shared)
    return shared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--working-directory")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        migrate(args.source, args.target, args.working_directory, args.apply)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"Migration refused: {type(exc).__name__}; check paths and configuration without overwriting existing files.\n")
    print("Migration written; source preserved. Services unchanged." if args.apply else
          "Migration validated; no files changed. Use --apply to write.")


if __name__ == "__main__":
    main()
