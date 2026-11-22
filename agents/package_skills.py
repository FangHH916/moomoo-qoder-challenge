"""Build and verify the one-zip-per-skill packages required by Handbook §4.2, offline.

Usage:
  python -B agents/package_skills.py --check     # validate sources, write nothing
  python -B agents/package_skills.py             # write agents/dist/<name>.zip and print hashes

The archives are byte-deterministic (fixed member timestamps and order, and members stored as the LF
canonical bytes that `agents/evaluate.py` hashes, so a `core.autocrlf=true` checkout packages identically),
so the SHA-256 recorded here can be bound to `expected_uploaded_archive_sha256` in release-lock.json before
an upload. Writing a zip is not uploading: no cloud call, credential read or Skill Center access happens.

A skill that ships runnable code carries a `skill.json` manifest naming the repository files it needs:

  {"seeds": ["live/golive.py"], "include": ["live/config.pstar.json"], "requirements": [...]}

`seeds` are followed through their own `import` statements, so the archive holds the whole transitive
runtime rather than a hand-copied list that rots, and every member keeps its repository-relative path:
unpacking the zip and setting `PYTHONPATH` to it gives the same `python -m live.golive` the local run uses.
The repository files stay the only source of truth -- a skill folder may not also hold a copy, because two
copies of `rules/gate_pstar.py` is exactly how a reviewed gate and an executed gate drift apart.
"""
from __future__ import annotations

import argparse
import ast
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
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")      # the platform's own skill-name shape
FIXED_STAMP = (1980, 1, 1, 0, 0, 0)
MAX_ARCHIVE_BYTES = 50_000_000
MAX_DESCRIPTION = 1024
MAX_BODY_LINES = 500
MANIFEST = "skill.json"
RUNTIME_RECORD = "RUNTIME.json"
# Six playbooks plus the Executor runtime. A skill is added or removed on purpose, so this number is too.
EXPECTED_SKILLS = 7
# Only these top-level packages are importable runtime; anything else a seed imports is stdlib or a
# third-party requirement and belongs in `requirements`, not in the archive.
REPO_PACKAGES = ("live", "rules", "journal", "agents", "skills")
MANIFEST_KEYS = ("seeds", "include", "requirements", "notes")


def canonical_bytes(path: Path) -> bytes:
    """The file's bytes with CRLF folded to LF, matching `agents/evaluate.py`'s source hashing."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def module_file(name: str):
    """The repository file a dotted import or a repository path names, or None when it is not repo code.

    Only `REPO_PACKAGES` are followed: an import of `exchange_calendars` is a requirement, not a file the
    archive can carry, and pretending otherwise would package a skill that fails on the first run. Seeds
    are given as paths, because a skill folder name may hold a hyphen and therefore never be importable.
    """
    text = str(name)
    if "/" in text or text.endswith(".py"):
        path = ROOT / text
        return path if path.is_file() else None
    parts = text.split(".")
    if not parts or parts[0] not in REPO_PACKAGES:
        return None
    candidate = ROOT.joinpath(*parts)
    for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
        if path.is_file():
            return path
    return None


def imported_names(path: Path) -> list[str]:
    """Every module this file names in an import, resolved against the repository where it can.

    `import a.b`, `from a.b import c` and the `c` that is itself a module all count; a name that turns out
    to be a function resolves to no file and is dropped by the caller. A file that does not parse is a
    packaging failure rather than a silent gap, because an unparsed import is an unshipped module.
    """
    try:
        tree = ast.parse(canonical_bytes(path).decode("utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise ValueError(f"{relative(path)}: cannot read its imports ({exc})") from None
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ValueError(f"{relative(path)}: relative import {node.module!r} has no packaged meaning")
            base = node.module or ""
            if base:
                names.append(base)
            names += [f"{base}.{alias.name}" for alias in node.names if base]
    return names


def closure(seeds: list[str]) -> list[str]:
    """Every repository module the seeds import, transitively, found by reading the seeds themselves.

    A hand-written list of runtime files is the thing that rots: add one import to `live/golive.py`, ship a
    skill that cannot start, and only the cloud run finds out. Resolving imports here means the archive is
    complete or the build refuses.
    """
    seen, pending, missing = set(), list(seeds), []
    while pending:
        name = pending.pop()
        path = module_file(name)
        if path is None:
            # An unresolvable name is either the standard library or a declared third-party requirement,
            # which no archive can carry, or repository code that would be missing at run time. The first
            # is normal; the second is a broken package, so a name whose first segment is a directory here
            # has to resolve.
            if name in seeds:
                missing.append(name)
            elif module_file(str(name).rsplit(".", 1)[0]) is None and (
                    ROOT / str(name).split(".")[0].split("/")[0]).is_dir():
                raise ValueError(f"{name}: imported by packaged code but resolves to no file under it -- a "
                                 f"runtime module cannot be left out of the archive")
            continue
        if path in seen:
            continue
        seen.add(path)
        pending += imported_names(path)
    if missing:
        raise ValueError("seed resolves to no repository module: " + ", ".join(sorted(missing)))
    for path in list(seen):                      # a package needs its own __init__.py to import at all
        for parent in path.parents:
            if parent == ROOT:
                break
            init = parent / "__init__.py"
            if init.is_file():
                seen.add(init)
    return sorted(relative(path) for path in seen)


def relative(path: Path) -> str:
    return Path(path).resolve().relative_to(ROOT).as_posix()


def read_manifest(folder: Path) -> dict | None:
    """The optional `skill.json`: the code this skill ships, named as repository paths."""
    path = folder / MANIFEST
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{folder.name}/{MANIFEST}: not valid JSON ({exc})") from None
    if not isinstance(payload, dict):
        raise ValueError(f"{folder.name}/{MANIFEST}: must be a JSON object")
    unknown = sorted(set(payload) - set(MANIFEST_KEYS))
    if unknown:
        raise ValueError(f"{folder.name}/{MANIFEST}: unsupported keys {', '.join(unknown)}")
    for key in ("seeds", "include", "requirements"):
        value = payload.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"{folder.name}/{MANIFEST}: {key} must be a list of non-empty strings")
    for key in ("seeds", "include"):
        for item in payload.get(key, ()):
            if not (ROOT / item).is_file():
                raise ValueError(f"{folder.name}/{MANIFEST}: {key} names {item}, which is not a file here")
            if Path(item).is_absolute() or ".." in Path(item).parts:
                raise ValueError(f"{folder.name}/{MANIFEST}: {item} must be a repository-relative path")
    if not payload.get("seeds"):
        raise ValueError(f"{folder.name}/{MANIFEST}: a manifest with no seeds ships no code")
    return payload


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
        raise ValueError(f"folder name {name!r} is not a lowercase skill name")
    source = folder / "SKILL.md"
    if not source.is_file():
        raise ValueError(f"{name}: SKILL.md is missing")
    text = canonical_bytes(source).decode("utf-8")
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
            members[path.relative_to(folder).as_posix()] = canonical_bytes(path)
    manifest = read_manifest(folder)
    runtime = None
    if manifest:
        runtime = sorted({*closure(manifest["seeds"]), *manifest.get("include", ())})
        clash = sorted(set(runtime) & set(members))
        if clash:
            raise ValueError(f"{name}: {', '.join(clash)} would be packaged twice, once from the repository "
                            f"and once from skills/{name}; the repository file is the only source of truth")
        for path in runtime:
            member = ROOT / path
            if not member.is_file():
                raise ValueError(f"{name}: {path} is named by {MANIFEST} but is not a file")
            members[path] = canonical_bytes(member)
        members[RUNTIME_RECORD] = json.dumps({
            "skill": name, "seeds": sorted(manifest["seeds"]), "include": sorted(manifest["include"]),
            "requirements": manifest.get("requirements", []), "notes": manifest.get("notes", ""),
            "runtime": runtime,
            "sha256": {path: hashlib.sha256(members[path]).hexdigest() for path in runtime}},
            indent=2, sort_keys=True).encode("utf-8") + b"\n"
    digest = hashlib.sha256()
    for member in sorted(members):
        digest.update(member.encode())
        digest.update(members[member])
    return {"name": name, "members": sorted(members), "bytes": members,
            "sources": {member: hashlib.sha256(payload).hexdigest() for member, payload in members.items()},
            **({"runtime": runtime, "requirements": manifest.get("requirements", [])} if manifest else {}),
            "content_sha256": digest.hexdigest()}


def build(entry: dict, out_dir: Path) -> Path:
    """Write the archive from the bytes `check_skill` already hashed, so nothing is read twice differently."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in entry["members"]:
            info = zipfile.ZipInfo(f"{entry['name']}/{member}", date_time=FIXED_STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, entry["bytes"][member])
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


def resolve_out(path: Path) -> Path:
    """Resolve `--out` against the repository, so a relative scratch directory reports a project-relative path."""
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate skill sources without writing archives")
    parser.add_argument("--out", type=Path, default=DIST)
    args = parser.parse_args(argv)
    out = resolve_out(args.out)
    folders = sorted(path for path in SKILLS.iterdir()
                     if path.is_dir() and not path.name.startswith(("_", ".")))
    report, failures = [], []
    for folder in folders:
        try:
            entry = check_skill(folder)
            if not args.check:
                archive = build(entry, out)
                entry["archive"] = str(archive.relative_to(ROOT).as_posix())
                entry["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            report.append(entry)
        except (ValueError, KeyError, OSError) as exc:
            failures.append(f"{folder.name}: {exc}")
    print(json.dumps({"mode": "check" if args.check else "build", "skills": [{key: value for key, value in
                                                                             entry.items()
                                                                             if key != "bytes"}
                                                                            for entry in report],
                      "uploaded": False, "cloud_write": False, "failures": failures},
                     indent=2, sort_keys=True))
    return 1 if failures or len(report) != EXPECTED_SKILLS else 0


if __name__ == "__main__":
    raise SystemExit(main())
