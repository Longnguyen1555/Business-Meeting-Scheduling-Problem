"""Check the research files against the repository's SHA-256 manifest."""
from pathlib import Path
import hashlib
import json
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "outputs/reproduction_manifest.json").read_text())
    errors = []
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.is_file():
            errors.append("Missing: " + relative)
        elif (path.stat().st_size != expected["bytes"]
              or hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]):
            errors.append("Changed: " + relative)
    print(json.dumps({"valid": not errors, "files_checked": len(manifest["files"]),
                      "errors": errors}, indent=2))
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
