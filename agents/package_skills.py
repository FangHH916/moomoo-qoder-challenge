"""Build and verify the one-zip-per-skill packages required by Handbook §4.2, offline.

Usage:
  python -B agents/package_skills.py --check     # validate sources, write nothing
  python -B agents/package_skills.py             # write agents/dist/<name>.zip and print hashes

The archives are byte-deterministic (fixed member timestamps and order), so the SHA-256 recorded
here can be bound to `expected_uploaded_archive_sha256` in release-lock.json before an upload.
Writing a zip is not uploading: no cloud call, credential read or Skill Center access happens.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
DIST = ROOT / "agents" / "dist"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FIXED_STAMP = (1980, 1, 1, 0, 0, 0)
MAX_ARCHIVE_BYTES = 50_000_000
MAX_DESCRIPTION = 1024
MAX_BODY_LINES = 500


def frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise ValueError("missing opening frontmatter fence")
    try:
        _, raw, body = text.split("---\n", 2)
    except ValueError:
        raise ValueError("missing closing frontmatter fence") from None
    fields = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep or line[:1].isspace():
            raise ValueError(f"unsupported frontmatter line: {line!r}")
        fields[key.strip()] = value.strip()
    return fields, body


def check_skill(folder: Path) -> dict:
    name = folder.name
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"folder name {name!r} is not lowercase hyphenated")
    source = folder / "SKILL.md"
    if not source.is_file():
        raise ValueError(f"{name}: SKILL.md is missing")
    text = source.read_text(encoding="utf-8")
    if re.search(r"\bTO" + r"DO:", text, re.I):
        raise ValueError(f"{name}: unresolved TODO placeholder")
    fields, body = frontmatter(text)
    if fields.get("name") != name:
        raise ValueError(f"{name}: frontmatter name {fields.get('name')!r} must match the folder")
    description = fields.get("description", "")
    if not description:
        raise ValueError(f"{name}: description is required")
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"{name}: description exceeds {MAX_DESCRIPTION} characters")
    if len(body.splitlines()) > MAX_BODY_LINES:
        raise ValueError(f"{name}: body exceeds {MAX_BODY_LINES} lines")
    members = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            members[path.relative_to(folder).as_posix()] = path.read_bytes()
    digest = hashlib.sha256()
    for member in sorted(members):
        digest.update(member.encode())
        digest.update(members[member])
    return {"name": name, "members": sorted(members),
            "sources": {member: hashlib.sha256(payload).hexdigest() for member, payload in members.items()},
            "content_sha256": digest.hexdigest()}


def build(entry: dict, folder: Path, out_dir: Path) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in entry["members"]:
            info = zipfile.ZipInfo(f"{entry['name']}/{member}", date_time=FIXED_STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, (folder / member).read_bytes())
    payload = buffer.getvalue()
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"{entry['name']}: archive exceeds 50MB")
    target = out_dir / f"{entry['name']}.zip"
    previous = target.read_bytes() if target.exists() else None
    if previous is not None and previous != payload:
        raise ValueError(f"{entry['name']}: existing archive differs; delete it deliberately")
    if previous is None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate skill sources without writing archives")
    parser.add_argument("--out", type=Path, default=DIST)
    args = parser.parse_args(argv)
    folders = sorted(path for path in SKILLS.iterdir()
                     if path.is_dir() and not path.name.startswith(("_", ".")))
    report, failures = [], []
    for folder in folders:
        try:
            entry = check_skill(folder)
            if not args.check:
                archive = build(entry, folder, args.out)
                entry["archive"] = str(archive.relative_to(ROOT).as_posix())
                entry["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            report.append(entry)
        except (ValueError, KeyError, OSError) as exc:
            failures.append(f"{folder.name}: {exc}")
    print(json.dumps({"mode": "check" if args.check else "build", "skills": report,
                      "uploaded": False, "cloud_write": False, "failures": failures},
                     indent=2, sort_keys=True))
    return 1 if failures or len(report) != 5 else 0


if __name__ == "__main__":
    raise SystemExit(main())
