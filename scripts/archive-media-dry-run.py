#!/usr/bin/env python3
"""Map Archive attachments to External File Embed syntax (dry-run by default).

Defaults to every file still in Attachments (the original five are already in
assets). Pass --apply to move files and rewrite notes.

  python3 scripts/archive-media-dry-run.py
  python3 scripts/archive-media-dry-run.py --json-out /tmp/archive-media-link-map.json
  python3 scripts/archive-media-dry-run.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

HOME = Path.home()
VAULT = HOME / "Obsidian/My Vault"
ATTACH = VAULT / "📁 400 🗄 Archive/📁 410 📎 Attachments"
ASSETS = HOME / "Obsidian/assets"

WIKI_RE = re.compile(
    r"(?P<bang>!?)\[\[(?P<target>[^\]|#]+)(?:#(?P<heading>[^\]|]+))?(?:\|(?P<alias>[^\]]+))?\]\]"
)
MD_RE = re.compile(
    r"(?P<bang>!?)\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)"
)
SKIP_DIRS = {".obsidian", ".trash", ".git"}
SKIP_NAMES = {".DS_Store"}


def remaining_filenames() -> list[str]:
    names = [
        path.name
        for path in sorted(ATTACH.iterdir())
        if path.is_file() and path.name not in SKIP_NAMES
    ]
    if not names:
        raise SystemExit(f"No files left in {ATTACH}")
    return names


def home_uri(dest: Path) -> str:
    return "home://" + dest.relative_to(HOME).as_posix()


def embed_block(uri: str, size: str | None) -> str:
    line = f"{uri}|{size}" if size else uri
    return f"```EmbedRelativeTo\n{line}\n```"


def link_html(uri: str, label: str) -> str:
    return f'<a href="#{uri}" class="LinkRelativeTo">{label}</a>'


def resolve_filename(raw: str, files: dict[str, Path]) -> str | None:
    text = raw.strip().strip("<>").split("?")[0]
    if text.startswith("http://") or text.startswith("https://"):
        return None
    name = Path(text).name
    if name not in files:
        return None
    if name == text or "410 📎 Attachments/" in text or text.endswith(name):
        return name
    return None


def replacement(filename: str, dest: Path, *, embed: bool, alias: str | None) -> str:
    uri = home_uri(dest)
    if embed:
        return embed_block(uri, alias)
    return link_html(uri, alias or filename)


def scan_note(path: Path, files: dict[str, Path]) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    hits: list[dict[str, str]] = []
    rel = str(path.relative_to(VAULT))

    def consider(match: re.Match[str], target: str, embed: bool, alias: str | None) -> None:
        filename = resolve_filename(target, files)
        if not filename:
            return
        hits.append(
            {
                "file": filename,
                "note": rel,
                "original": match.group(0),
                "replacement": replacement(
                    filename, files[filename], embed=embed, alias=alias
                ),
            }
        )

    for match in WIKI_RE.finditer(text):
        consider(
            match,
            match.group("target"),
            match.group("bang") == "!",
            match.group("alias"),
        )
    for match in MD_RE.finditer(text):
        consider(
            match,
            match.group("target"),
            match.group("bang") == "!",
            match.group("alt") or None,
        )
    return hits


def iter_notes() -> list[Path]:
    notes: list[Path] = []
    for path in VAULT.rglob("*.md"):
        if SKIP_DIRS.intersection(path.relative_to(VAULT).parts):
            continue
        notes.append(path)
    return notes


def build_map(filenames: list[str]) -> tuple[dict[str, Path], list[dict[str, str]]]:
    files: dict[str, Path] = {}
    missing: list[str] = []
    for name in filenames:
        src = ATTACH / name
        if not src.is_file():
            missing.append(name)
            continue
        files[name] = ASSETS / name
    if missing:
        raise SystemExit("Not in Attachments:\n  " + "\n  ".join(missing))

    hits: list[dict[str, str]] = []
    for note in iter_notes():
        hits.extend(scan_note(note, files))
    return files, hits


def apply(files: dict[str, Path], hits: list[dict[str, str]]) -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, dest in files.items():
        src = ATTACH / name
        if dest.exists():
            raise SystemExit(f"Refusing to overwrite {dest}")
        shutil.move(str(src), str(dest))

    by_note: dict[str, list[dict[str, str]]] = defaultdict(list)
    for hit in hits:
        by_note[hit["note"]].append(hit)
    for rel, note_hits in by_note.items():
        path = VAULT / rel
        text = path.read_text(encoding="utf-8")
        for hit in note_hits:
            if hit["original"] not in text:
                raise SystemExit(f"Lost original in {rel}: {hit['original']!r}")
            text = text.replace(hit["original"], hit["replacement"], 1)
        path.write_text(text, encoding="utf-8")


def payload_for(files: dict[str, Path], hits: list[dict[str, str]]) -> dict:
    refs = defaultdict(int)
    for hit in hits:
        refs[hit["file"]] += 1
    return {
        "source": str(ATTACH),
        "destination": str(ASSETS),
        "files": [
            {
                "name": name,
                "from": str(ATTACH / name),
                "to": str(dest),
                "uri": home_uri(dest),
                "refs": refs[name],
            }
            for name, dest in files.items()
        ],
        "rewrites": hits,
    }


def print_map(
    payload: dict,
    *,
    as_json: bool,
    json_out: Path | None,
    verbose: bool,
) -> None:
    if json_out:
        json_out.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if as_json:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return

    files = payload["files"]
    hits = payload["rewrites"]
    linked = [item for item in files if item["refs"]]
    orphan = [item for item in files if not item["refs"]]
    notes = {hit["note"] for hit in hits}
    print(f"Source: {payload['source']}")
    print(f"Dest:   {payload['destination']}")
    print(
        f"{len(files)} files · {len(linked)} with note hits · "
        f"{len(orphan)} unreferenced · {len(hits)} rewrites in {len(notes)} notes"
    )
    if json_out:
        print(f"JSON:   {json_out}")
    if verbose or len(files) <= 12:
        print()
        for item in files:
            print(f"{item['name']}  ({item['refs']} note hit(s))")
            print(f"  {item['uri']}")
        if hits:
            print("\nLink map")
            for hit in hits:
                print(f"\n{hit['note']}")
                print(f"  - {hit['original']}")
                for line in hit["replacement"].splitlines():
                    print(f"    {line}")
        return
    print()
    print("Sample rewrites")
    for hit in hits[:8]:
        print(f"  {hit['note']}")
        print(f"    {hit['original']}")
    if orphan:
        print(f"\nUnreferenced ({len(orphan)}); still moved on --apply:")
        for item in orphan[:12]:
            print(f"  {item['name']}")
        if len(orphan) > 12:
            print(f"  … {len(orphan) - 12} more")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--files",
        nargs="+",
        help="Attachment filenames (default: everything still in Attachments)",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    parser.add_argument("--json-out", type=Path, help="Write the full link map to this path")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every file and rewrite even when the set is large",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Move the files and rewrite notes (default is dry-run)",
    )
    args = parser.parse_args()
    filenames = args.files or remaining_filenames()
    files, hits = build_map(filenames)
    payload = payload_for(files, hits)
    print_map(
        payload,
        as_json=args.json,
        json_out=args.json_out,
        verbose=args.verbose,
    )
    if args.apply:
        apply(files, hits)
        print("\nApplied moves and rewrites.", file=sys.stderr)
    elif not args.json:
        print("\nDry-run only. Pass --apply to move files and rewrite notes.")


if __name__ == "__main__":
    main()
