"""Write the GitHub release notes for one version from CHANGELOG.json.

    python scripts/release_notes.py 1.0.0 --out release_notes.md
    python scripts/release_notes.py 1.0.0            # to stdout, for a look

Used by .github/workflows/release.yml; also handy for drafting by hand.

IMPORTANT: always use --out for anything that is published. Piping stdout
through PowerShell on the CI runner re-decoded the em dashes and curly
quotes as "?" (the v1.0.1 and v1.0.2 release pages shipped that way) because
Python's console encoding there is not UTF-8. --out writes the file as
UTF-8 directly and no shell touches the bytes.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(version: str, out_path: "str | None" = None) -> int:
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
    text = "\n".join(out) + "\n"
    if out_path is not None:
        Path(out_path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    out_file = None
    if "--out" in args:
        i = args.index("--out")
        out_file = args[i + 1]
        del args[i:i + 2]
    sys.exit(main(args[0].lstrip("vV") if args else "", out_file))
