"""CLI entrypoint for the in-sandbox multi-target index merge.

Invoked by the addon bootstrap as::

    python3 -m strix_code_graph.merge <dest.sqlite> <label> <src.sqlite> [<label> <src> ...]

Kept as a real module with an ``argv`` interface (rather than an inline
``python3 -c "..."`` string) so bootstrap composes ONE ``session.exec`` argument
list — no nested shell/python quoting, and a target label containing a quote or
shell metacharacter can't break or inject into the command.

``label``/``src`` are supplied in pairs; a ``label`` may be empty ("") for the
bare-``/workspace`` target — passed as the literal empty string in argv.
Missing source paths are skipped (a target whose index never built).
"""

from __future__ import annotations

import sys
from pathlib import Path

from .indexer import merge_sqlite_indexes


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or (len(args) - 1) % 2 != 0:
        print(
            "usage: python -m strix_code_graph.merge <dest> <label> <src> [<label> <src> ...]",
            file=sys.stderr,
        )
        return 2
    dest = Path(args[0])
    pairs = args[1:]
    sources: list[tuple[str, Path]] = [
        (pairs[i], Path(pairs[i + 1])) for i in range(0, len(pairs), 2)
    ]
    real = [(label, path) for label, path in sources if path.exists()]
    if not real:
        print("strix_code_graph.merge: no source indexes present", file=sys.stderr)
        return 1
    merge_sqlite_indexes(real, dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
