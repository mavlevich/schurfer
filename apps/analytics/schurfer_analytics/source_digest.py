"""SHA-256 over the Python sources of a package directory.

Lets a one-shot production command prove the image it runs was built from the checked-out
revision: the host hashes `apps/analytics/schurfer_analytics` in the checkout, the image
hashes its installed `schurfer_analytics`, and the two must match. Standard library only
and free of package imports, so the host runs this same file with a bare `python3`.

The digest covers every `*.py` file under the directory, by relative path and bytes, in
sorted order; caches and other files are ignored.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def source_digest(root: Path) -> str:
    files = sorted(
        path for path in root.rglob("*.py") if "__pycache__" not in path.relative_to(root).parts
    )
    if not files:
        raise ValueError(f"no Python source under {root}")
    digest = hashlib.sha256()
    for path in files:
        name = path.relative_to(root).as_posix().encode()
        body = path.read_bytes()
        digest.update(len(name).to_bytes(4, "big") + name)
        digest.update(len(body).to_bytes(8, "big") + body)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path(__file__).resolve().parent
    sys.stdout.write(source_digest(root) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
