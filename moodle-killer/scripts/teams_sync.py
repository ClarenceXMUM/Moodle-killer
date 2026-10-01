#!/usr/bin/env python3
"""Scan locally synced Teams/OneDrive folders and copy useful course files.

No Teams credentials or Graph API are needed. OneDrive owns the remote sync;
this module only reads the local folder and maintains its own copy state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import config_store as cs

DEFAULT_FOLDERS = {
    "01 Class Materials": "Class Materials",
    "02 General": "Course Information",
    "03 Assignments": "Assignments",
    "04 Projects and Presentation": "Projects and Presentation",
    "05 Test": "Test",
    "06 Final Exam": "Final Exam",
    "07 Textbook": "Textbook",
}


def sources_path():
    return cs.home() / "teams_sources.json"


def load_sources():
    path = sources_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("teams_sources.json must be an object")
    return data


def save_sources(sources):
    """写回 teams_sources.json（例如 mk folder --apply 改了落地目录之后）。"""
    path = sources_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sources or {}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _state_path(key):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    return cs.state_dir() / ("teams_%s.json" % safe)


def _save_state(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".teams-state-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _copy_verified(src, dst, digest):
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".teams-copy-", dir=str(dst.parent))
    os.close(fd)
    try:
        shutil.copyfile(src, temp_name)
        if _digest(Path(temp_name)) != digest:
            raise OSError("copy verification failed: %s" % src)
        os.replace(temp_name, dst)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def scan_source(key, info, download=True):
    """Return {new_files, notes, scanned}; commit state only after a good copy."""
    if not isinstance(info, dict):
        raise ValueError("source entry must be an object")
    source = Path(os.path.expanduser(info["source"]))
    dest = Path(os.path.expanduser(info["path"]))
    folders = info.get("folders", DEFAULT_FOLDERS)
    if not source.is_dir():
        raise FileNotFoundError("Teams/OneDrive folder unavailable: %s" % source)
    if not folders or not isinstance(folders, dict):
        raise ValueError("folders must map source folder names to destination names")
    state_path = _state_path(key)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    known = state.get("files", {})
    new_files, notes = [], []
    scanned = 0
    for source_folder, dest_folder in folders.items():
        if (not source_folder or not dest_folder or source_folder in (".", "..")
                or dest_folder in (".", "..") or Path(source_folder).name != source_folder
                or Path(dest_folder).name != dest_folder):
            raise ValueError("folder mappings must be single directory names")
        root = source / source_folder
        if not root.exists():
            continue  # Empty or not yet synchronized section.
        if not root.is_dir():
            notes.append("不是文件夹: %s" % root)
            continue
        for src in sorted(root.rglob("*")):
            if (src.is_symlink() or not src.is_file() or src.name.startswith(".")
                    or any(part.startswith(".") for part in src.relative_to(root).parts)):
                continue
            scanned += 1
            rel = Path(dest_folder) / src.relative_to(root)
            dst = dest / rel
            label = rel.as_posix()
            try:
                digest = _digest(src)  # Reading forces an on-demand OneDrive file to hydrate.
                old = known.get(label)
                if old == digest and dst.is_file() and _digest(dst) == digest:
                    continue
                if dst.exists() and (old is None or _digest(dst) != old):
                    if _digest(dst) == digest:
                        if download:
                            known[label] = digest
                            _save_state(state_path, {"files": known})
                        continue
                    notes.append("目标已有不同内容，未覆盖: %s" % dst)
                    continue
                if download:
                    _copy_verified(src, dst, digest)
                    known[label] = digest
                    _save_state(state_path, {"files": known})
                new_files.append(label)
            except (OSError, ValueError) as exc:
                notes.append("复制失败 %s: %s" % (src, exc))
    return {"new_files": new_files, "notes": notes, "scanned": scanned}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Copy useful local Teams files into Knowledge")
    ap.add_argument("--dry-run", action="store_true", help="List changes without copying or writing state")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    results = {}
    for key, info in load_sources().items():
        try:
            if info.get("mute"):
                continue
            results[key] = scan_source(key, info, download=not args.dry_run)
        except (OSError, ValueError, KeyError, AttributeError) as exc:
            results[key] = {"new_files": [], "notes": [str(exc)], "scanned": 0}
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for key, result in results.items():
            print("%s: scanned %d, new %d" % (key, result["scanned"], len(result["new_files"])))
            for name in result["new_files"]:
                print("  + %s" % name)
            for note in result["notes"]:
                print("  ❌ %s" % note)
    return 2 if any(result["notes"] for result in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
