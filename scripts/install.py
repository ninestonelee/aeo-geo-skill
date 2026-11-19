#!/usr/bin/env python3
"""Install the classroom skill without overwriting an existing installation."""

import argparse
import os
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", choices=["codex", "claude"], default="codex")
    parser.add_argument("--dest", type=Path, help="Full destination skill directory")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent.parent / "aeo-geo"
    if args.dest:
        target = args.dest.expanduser().resolve()
        if args.dest.expanduser().is_symlink():
            parser.error("Destination is an existing symbolic link; no changes made.")
    elif args.app == "codex":
        target = Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")) / "skills" / "aeo-geo"
    else:
        target = Path.home() / ".claude" / "skills" / "aeo-geo"
    if target.exists() or target.is_symlink():
        parser.error(f"Destination already exists; no changes made: {target}")
    if not (source / "SKILL.md").is_file():
        parser.error("The source skill is missing SKILL.md.")
    if any(path.is_symlink() for path in source.rglob("*")):
        parser.error("The source contains a symbolic link; no changes made.")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    print(f"Installed: {target}")
    print("Use aeo-geo on your next message, or open a new chat if needed.")
    print("No API calls, key loading, notifications or scheduled tasks were performed.")


if __name__ == "__main__":
    main()
