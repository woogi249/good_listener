"""Fail when common credential formats appear in tracked or untracked source files."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


PATTERNS = (
    re.compile("sk" + r"-[A-Za-z0-9_-]{12,}"),
    re.compile("ghp" + r"_[A-Za-z0-9]{20,}"),
    re.compile("AKIA" + r"[0-9A-Z]{16}"),
)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
    )
    hits: list[str] = []
    for raw_name in output.split(b"\0"):
        if not raw_name:
            continue
        relative = raw_name.decode("utf-8", errors="surrogateescape")
        path = root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(pattern.search(text) for pattern in PATTERNS):
            hits.append(relative)

    if hits:
        print("Credential-like value found in tracked or untracked source files:", file=sys.stderr)
        for hit in hits:
            print(f"- {hit}", file=sys.stderr)
        return 1
    print("OK: no credential-like values in tracked or untracked source files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
