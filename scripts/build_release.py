#!/usr/bin/env python3
"""Build a deterministic, allowlisted classroom ZIP and SHA-256 checksum."""

import argparse
import hashlib
from pathlib import Path
import zipfile

PUBLIC_FILES = (
    "SKILL.md",
    "VERSION",
    "사용설명서.md",
    "scripts/aeo_service.py",
    "templates/questions.json",
    "templates/.env.example",
    "references/AEO_GEO_실전핸드북.md",
    "references/prompts/GEO_리라이트_프롬프트.md",
    "references/prompts/질문패널_생성_프롬프트.md",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v1.0.0")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    source = root / "aeo-geo"
    expected_version = "v" + (source / "VERSION").read_text().strip()
    if args.version != expected_version:
        parser.error(f"Version must match aeo-geo/VERSION: {expected_version}")
    output = args.output_dir or root / "dist"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "aeo-geo-skill.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for relative in sorted(PUBLIC_FILES):
            path = source / relative
            if path.is_symlink() or not path.is_file():
                parser.error(f"Missing or unsafe release file: {relative}")
            info = zipfile.ZipInfo("aeo-geo/" + relative, date_time=(2026, 11, 20, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = output / "SHA256SUMS"
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"Built {args.version}: {archive}")
    print(f"SHA256: {digest}")
    print(f"Checksum: {checksum}")


if __name__ == "__main__":
    main()
