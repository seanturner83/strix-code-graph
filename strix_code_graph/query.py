"""SQLite-backed code-graph query layer.

Opens the SQLite index produced by `indexer.build_index` and exposes the
five primitives the Strix tool layer needs:

  * find_definition(symbol)      → file:line:col where the symbol is defined
  * find_references(symbol)      → file:line ranges where it's used
  * find_implementations(iface)  → defs of subtypes (W2: relationship-blob
                                   parsing deferred; W2 stubs and returns
                                   empty list)
  * get_imports(file)            → other symbols this file imports
  * get_symbol_at(file, line)    → symbol moniker at that line

SCIP SQLite schema (produced by `scip expt-convert`):
  documents(id, language, relative_path, position_encoding, text)
  global_symbols(id, symbol, display_name, kind, documentation, ...)
  chunks(id, document_id, chunk_index, start_line, end_line, occurrences)
  mentions(chunk_id, symbol_id, role)         -- role 1=def, 0=ref
  defn_enclosing_ranges(symbol_id, document_id, start_line, start_char,
                        end_line, end_char)    -- not populated for every
                                                  symbol (re-exports lack
                                                  bodies); fall back to
                                                  mentions+chunks.

Design notes:
  * Symbol monikers in SCIP look like
      "scip-typescript npm portal-api HEAD src/auth/`index.ts`/foo."
    The LLM will query with bare names like "authorizeToParticipantAndAdminRole".
    We match via LIKE %name% which is fast enough at our scale (~5k symbols
    per repo); the moniker has its own indexed column.
  * SCIP's role enum is a bitfield in the spec but expt-convert collapses
    to 0/1 in practice. Treat 1 as definition, 0 as reference.
  * Results capped at MAX_ROWS per call. Higher limits are available via
    explicit kwarg for callers that need them (tests), but the LLM-facing
    tool wrappers (W2.2) lock the cap.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


MAX_ROWS = 50


# SCIP SymbolRole is a bitfield (scip.proto): Definition=0x1, Import=0x2,
# WriteAccess=0x4, ReadAccess=0x8, Generated=0x10, Test=0x20,
# ForwardDefinition=0x40. expt-convert preserves the bitfield verbatim.
# `find_references` wants anything that ISN'T a Definition (so reads,
# writes, imports all count as references) — `role != 0 AND (role & 1) = 0`.
ROLE_DEFINITION_MASK = 1  # bit 0
ROLE_DEFINITION = 1  # back-compat for callers that imported the constant


def _read_varint(blob: bytes, start: int) -> tuple[int, int]:
    """Decode one proto varint at offset `start`. Returns (value, next_offset)."""
    result = 0
    shift = 0
    i = start
    n = len(blob)
    while i < n:
        b = blob[i]
        result |= (b & 0x7F) << shift
        i += 1
        if not (b & 0x80):
            return result, i
        shift += 7
    return result, i


def _parse_one_relationship(blob: bytes, start: int, end: int) -> tuple[str | None, bool]:
    """Parse fields of one SCIP Relationship message from blob[start:end]."""
    symbol: str | None = None
    is_impl = False
    pos = start
    while pos < end:
        tag, pos = _read_varint(blob, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:  # varint
            val, pos = _read_varint(blob, pos)
            if field_num == 3:  # is_implementation
                is_impl = bool(val)
            # is_reference (2), is_type_definition (4), is_definition (5):
            # ignored — we only care about is_implementation
        elif wire_type == 2:  # length-delimited
            length, pos = _read_varint(blob, pos)
            field_end = pos + length
            if field_end > end:
                break
            if field_num == 1:  # symbol string
                symbol = blob[pos:field_end].decode("utf-8", errors="replace")
            pos = field_end
        else:
            # Unknown wire type — bail safely
            break
    return symbol, is_impl


def _parse_scip_relationships(blob: bytes) -> Iterator[tuple[str, bool]]:
    """Hand-rolled minimal parser for SCIP's `relationships` column —
    a repeated Relationship submessage serialized inline.

    SCIP Relationship (proto3):
      string symbol           = 1;   // wire tag 0x0a, length-delimited
      bool   is_reference     = 2;   // wire tag 0x10, varint
      bool   is_implementation= 3;   // wire tag 0x18, varint
      bool   is_type_definition=4;   // wire tag 0x20, varint
      bool   is_definition    = 5;   // wire tag 0x28, varint

    The container field on global_symbols' parent is a `repeated
    Relationship`. proto3 serialises that as a sequence of
    length-delimited submessage chunks (each Relationship preceded by
    its own varint length).

    Yields (symbol, is_implementation) per Relationship. Robust to
    unknown fields and ragged tails (returns what it has and stops).
    """
    pos = 0
    n = len(blob)
    while pos < n:
        length, pos = _read_varint(blob, pos)
        msg_end = pos + length
        if msg_end > n or length == 0:
            break
        symbol, is_impl = _parse_one_relationship(blob, pos, msg_end)
        if symbol:
            yield symbol, is_impl
        pos = msg_end


@dataclass(frozen=True)
class Location:
    """A file + line range. For find_definition we have precise start_char
    when defn_enclosing_ranges is populated; otherwise we fall back to the
    chunk's line range and start_char is None."""

    file: str
    start_line: int
    end_line: int
    start_char: int | None = None
    end_char: int | None = None

    def render(self) -> str:
        if self.start_char is not None:
            return f"{self.file}:{self.start_line}:{self.start_char}"
        if self.start_line == self.end_line:
            return f"{self.file}:{self.start_line}"
        return f"{self.file}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class SymbolMatch:
    """A symbol moniker the index knows about. `display_name` is what we
    extract from the tail of the moniker (scip-typescript leaves the
    proper display_name column empty)."""

    symbol: str
    display_name: str


class CodeGraphIndex:
    """Read-only handle on a SCIP-converted SQLite index."""

    def __init__(self, sqlite_path: Path) -> None:
        if not sqlite_path.exists():
            raise FileNotFoundError(f"code_graph index missing at {sqlite_path}")
        # uri=true + mode=ro: prevent accidental writes; SQLite will
        # surface readonly attempts as exceptions.
        uri = f"file:{sqlite_path}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.sqlite_path = sqlite_path

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    @staticmethod
    def _extract_display_name(symbol: str) -> str:
        """SCIP monikers look like
              "scip-typescript npm portal-api HEAD src/foo/`bar.ts`/baz."
        The trailing token after the last `/` or `` ` `` is the identifier
        the LLM will query for. We strip the trailing scip-suffix sigil
        (`.`, `#`, `()` if present) so equality compares cleanly.
        """
        tail = symbol.rstrip(".#()/")
        # Symbol fragments are separated by `/`. Take the rightmost.
        if "/" in tail:
            tail = tail.rsplit("/", 1)[-1]
        # scip-typescript wraps file names in backticks; strip if leftover.
        return tail.strip("`")

    def _resolve_symbol(self, name: str) -> list[SymbolMatch]:
        """Find global_symbols rows whose moniker contains `name` as a
        token. Returns up to MAX_ROWS matches sorted by symbol length
        (shorter = more specific match first, since trailing extras
        like `.` `()` make canonical names slightly longer)."""
        like = f"%{name}%"
        with self._cursor() as cur:
            cur.execute(
                "SELECT symbol FROM global_symbols WHERE symbol LIKE ? "
                "ORDER BY LENGTH(symbol) ASC LIMIT ?",
                (like, MAX_ROWS),
            )
            rows = cur.fetchall()
        matches = [
            SymbolMatch(
                symbol=row["symbol"],
                display_name=self._extract_display_name(row["symbol"]),
            )
            for row in rows
        ]
        # Exact display-name match wins; everything else stays in length
        # order. The LLM tool wrapper presents only exact matches by
        # default and exposes "did-you-mean" partials via a separate
        # listing.
        exact = [m for m in matches if m.display_name == name]
        partial = [m for m in matches if m.display_name != name]
        return exact + partial

    # ------------------------------------------------------------------
    # Public query methods (called by the W2.2 tool wrappers)
    # ------------------------------------------------------------------

    def find_definition(
        self, name: str, *, limit: int = MAX_ROWS
    ) -> list[tuple[SymbolMatch, Location]]:
        """Return (symbol, location) pairs for definitions matching `name`.

        Prefers `defn_enclosing_ranges` (precise file:line:col) but falls
        back to mentions with role=1 + chunk range when ranges are absent
        (common for re-exports and barrel files).
        """
        results: list[tuple[SymbolMatch, Location]] = []
        for match in self._resolve_symbol(name):
            with self._cursor() as cur:
                # Try defn_enclosing_ranges first.
                cur.execute(
                    """
                    SELECT d.relative_path, der.start_line, der.start_char,
                           der.end_line, der.end_char
                    FROM defn_enclosing_ranges der
                    JOIN documents d  ON d.id = der.document_id
                    JOIN global_symbols gs ON gs.id = der.symbol_id
                    WHERE gs.symbol = ?
                    LIMIT ?
                    """,
                    (match.symbol, limit),
                )
                rows = cur.fetchall()
                if rows:
                    for r in rows:
                        results.append((
                            match,
                            Location(
                                file=r["relative_path"],
                                start_line=r["start_line"],
                                start_char=r["start_char"],
                                end_line=r["end_line"],
                                end_char=r["end_char"],
                            ),
                        ))
                    continue
                # Fall back: definition via mentions role=1.
                cur.execute(
                    """
                    SELECT d.relative_path, c.start_line, c.end_line
                    FROM mentions m
                    JOIN chunks c     ON c.id = m.chunk_id
                    JOIN documents d  ON d.id = c.document_id
                    JOIN global_symbols gs ON gs.id = m.symbol_id
                    WHERE gs.symbol = ? AND m.role = ?
                    ORDER BY d.relative_path, c.start_line
                    LIMIT ?
                    """,
                    (match.symbol, ROLE_DEFINITION, limit),
                )
                for r in cur.fetchall():
                    results.append((
                        match,
                        Location(
                            file=r["relative_path"],
                            start_line=r["start_line"],
                            end_line=r["end_line"],
                        ),
                    ))
            if len(results) >= limit:
                break
        return results[:limit]

    def find_references(
        self, name: str, *, limit: int = MAX_ROWS, include_definition: bool = False
    ) -> list[tuple[SymbolMatch, Location]]:
        """Return all references to symbols matching `name`. A reference is
        any non-zero SCIP role that doesn't have the Definition bit (0x1)
        set — i.e. Read (0x8), Write (0x4), Import (0x2), or any
        combination. The definition site is included when
        `include_definition` is set."""
        results: list[tuple[SymbolMatch, Location]] = []
        if include_definition:
            role_clause = "m.role != 0"
        else:
            role_clause = "m.role != 0 AND (m.role & ?) = 0"

        for match in self._resolve_symbol(name):
            with self._cursor() as cur:
                params: tuple = (match.symbol,)
                if not include_definition:
                    params = params + (ROLE_DEFINITION_MASK,)
                params = params + (limit,)
                cur.execute(
                    f"""
                    SELECT d.relative_path, c.start_line, c.end_line
                    FROM mentions m
                    JOIN chunks c     ON c.id = m.chunk_id
                    JOIN documents d  ON d.id = c.document_id
                    JOIN global_symbols gs ON gs.id = m.symbol_id
                    WHERE gs.symbol = ? AND {role_clause}
                    ORDER BY d.relative_path, c.start_line
                    LIMIT ?
                    """,
                    params,
                )
                for r in cur.fetchall():
                    results.append((
                        match,
                        Location(
                            file=r["relative_path"],
                            start_line=r["start_line"],
                            end_line=r["end_line"],
                        ),
                    ))
            if len(results) >= limit:
                break
        return results[:limit]

    def find_implementations(
        self, name: str, *, limit: int = MAX_ROWS
    ) -> list[tuple[SymbolMatch, Location]]:
        """Return symbols that implement / subtype the given name.

        SCIP encodes inheritance relationships in
        `global_symbols.relationships`, a protobuf-encoded repeated
        `Relationship` message:

          message Relationship {
            string symbol           = 1;   // wire tag 0x0a, length-delimited
            bool   is_reference     = 2;   // wire tag 0x10, varint
            bool   is_implementation= 3;   // wire tag 0x18, varint
            bool   is_type_definition=4;   // wire tag 0x20, varint
            bool   is_definition    = 5;   // wire tag 0x28, varint
          }

        A symbol with a Relationship pointing at `name` with
        is_implementation=true is one of name's implementors.
        We scan all global_symbols, parse their relationships blob,
        and return rows whose blob references our target with the
        implementation flag set.
        """
        if not name:
            return []
        matches = self._resolve_symbol(name)
        if not matches:
            return []
        target_symbols = {m.symbol for m in matches}

        results: list[tuple[SymbolMatch, Location]] = []
        with self._cursor() as cur:
            cur.execute(
                "SELECT id, symbol, relationships FROM global_symbols "
                "WHERE relationships IS NOT NULL"
            )
            rows = cur.fetchall()
        for row in rows:
            blob = row["relationships"]
            if not blob:
                continue
            for rel_target, is_impl in _parse_scip_relationships(blob):
                if not is_impl:
                    continue
                if rel_target in target_symbols:
                    # Look up definition location of the implementor row
                    with self._cursor() as cur:
                        cur.execute(
                            """
                            SELECT d.relative_path, der.start_line,
                                   der.start_char, der.end_line, der.end_char
                            FROM defn_enclosing_ranges der
                            JOIN documents d ON d.id = der.document_id
                            WHERE der.symbol_id = ?
                            LIMIT 1
                            """,
                            (row["id"],),
                        )
                        loc_row = cur.fetchone()
                    if not loc_row:
                        continue
                    results.append(
                        (
                            SymbolMatch(
                                symbol=row["symbol"],
                                display_name=self._extract_display_name(row["symbol"]),
                            ),
                            Location(
                                file=loc_row["relative_path"],
                                start_line=loc_row["start_line"],
                                start_char=loc_row["start_char"],
                                end_line=loc_row["end_line"],
                                end_char=loc_row["end_char"],
                            ),
                        )
                    )
                    if len(results) >= limit:
                        return results
                    break  # one Relationship per row is enough
        return results

    def list_symbols(
        self, scope: str, *, limit: int = MAX_ROWS
    ) -> list[tuple[SymbolMatch, Location]]:
        """List defined symbols whose containing document path begins
        with `scope` (file path or directory prefix). Returns the
        definition site for each symbol — useful for "what's in this
        module" triage without reading the whole file."""
        if not scope:
            return []
        like_pattern = f"{scope.rstrip('/')}%"
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT gs.symbol, d.relative_path,
                       der.start_line, der.start_char,
                       der.end_line, der.end_char
                FROM defn_enclosing_ranges der
                JOIN documents d ON d.id = der.document_id
                JOIN global_symbols gs ON gs.id = der.symbol_id
                WHERE d.relative_path LIKE ?
                ORDER BY d.relative_path, der.start_line
                LIMIT ?
                """,
                (like_pattern, limit),
            )
            return [
                (
                    SymbolMatch(
                        symbol=r["symbol"],
                        display_name=self._extract_display_name(r["symbol"]),
                    ),
                    Location(
                        file=r["relative_path"],
                        start_line=r["start_line"],
                        start_char=r["start_char"],
                        end_line=r["end_line"],
                        end_char=r["end_char"],
                    ),
                )
                for r in cur.fetchall()
            ]

    def get_imports(self, file: str, *, limit: int = MAX_ROWS) -> list[SymbolMatch]:
        """Return the global symbols mentioned in `file`. SCIP doesn't tag
        imports specifically; what we can do without decoding the
        occurrences blob is enumerate distinct symbols referenced from
        that file's chunks. That's a superset of imports (includes
        in-file refs to globals) but useful for "what does this file
        depend on" questions."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT gs.symbol
                FROM mentions m
                JOIN chunks c    ON c.id = m.chunk_id
                JOIN documents d ON d.id = c.document_id
                JOIN global_symbols gs ON gs.id = m.symbol_id
                WHERE d.relative_path = ? AND m.role != 0 AND (m.role & ?) = 0
                ORDER BY gs.symbol
                LIMIT ?
                """,
                (file, ROLE_DEFINITION_MASK, limit),
            )
            return [
                SymbolMatch(
                    symbol=r["symbol"],
                    display_name=self._extract_display_name(r["symbol"]),
                )
                for r in cur.fetchall()
            ]

    def get_symbol_at(self, file: str, line: int) -> list[SymbolMatch]:
        """Return the symbols mentioned in the chunk that contains `line`
        in `file`. Coarser than what an IDE gives you — we return every
        symbol in the chunk rather than the one under the cursor —
        because we don't decode the occurrence blob in W2. Still useful
        for 'what is this code talking about' lookups."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT gs.symbol
                FROM chunks c
                JOIN documents d  ON d.id = c.document_id
                JOIN mentions m   ON m.chunk_id = c.id
                JOIN global_symbols gs ON gs.id = m.symbol_id
                WHERE d.relative_path = ?
                  AND c.start_line <= ? AND c.end_line >= ?
                ORDER BY gs.symbol
                LIMIT ?
                """,
                (file, line, line, MAX_ROWS),
            )
            return [
                SymbolMatch(
                    symbol=r["symbol"],
                    display_name=self._extract_display_name(r["symbol"]),
                )
                for r in cur.fetchall()
            ]

    # ------------------------------------------------------------------
    # Index discovery helpers
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, root: Path | None = None) -> CodeGraphIndex | None:
        """Find the most-recently-built index under `root` and open it.

        ``root`` defaults to ``$STRIX_CODE_GRAPH_DIR`` (set by the addon
        bootstrap to the runner-local directory the sandbox-built index was
        copied out to), falling back to ``./strix_code_graph`` under the cwd.

        Returns None if no index exists — the query tools degrade gracefully to
        "code graph unavailable" when the index build was skipped or failed.
        """
        if root is None:
            root = Path(os.environ.get("STRIX_CODE_GRAPH_DIR", "strix_code_graph"))
        if not root.exists():
            return None
        candidates = sorted(
            root.glob("*/code_graph.sqlite"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None
        return cls(candidates[0])
