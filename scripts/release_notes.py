"""Print the GitHub release notes for one version from CHANGELOG.json.

    python scripts/release_notes.py 1.0.0 > release_notes.md

Used by .github/workflows/release.yml; also handy for drafting by hand.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(version: str) -> int:
    changelog = json.loads((ROOT / "CHANGELOG.json").read_text(encoding="utf-8"))
    entries = [e for e in changelog["changelog"] if e.get("version") == version]
    if not entries:
        print(f"No CHANGELOG.json entry carries version {version!r}", file=sys.stderr)
        return 1
    out = []
    for entry in entries:
        out.append(f"## {entry['category']} — {entry['date']}\n")
        out.extend(f"- {change}" for change in entry["changes"])
        out.append("")
    out.append("**Windows will warn you the first time you run the installer** (it isn't code-signed). "
               "Click **More info**, then **Run anyway**. See the README for why.")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1].lstrip("vV") if len(sys.argv) > 1 else ""))
