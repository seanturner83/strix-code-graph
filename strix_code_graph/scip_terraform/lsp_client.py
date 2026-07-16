"""Minimal synchronous LSP client over a language-server subprocess (stdio).

Just enough of the Language Server Protocol to drive `terraform-ls serve` for
indexing: initialize, open documents, and issue documentSymbol / definition /
references requests. NOT a general LSP library — no streaming diagnostics, no
cancellation, no dynamic capability registration. Synchronous request/response
keyed on the JSON-RPC `id`; server-initiated requests/notifications that arrive
while we wait are drained and ignored (we register no capabilities that require
answering them).

Decoupled from strix on purpose (view-to-OSS): depends only on the stdlib + a
server binary path. The SCIP emission lives in `emit.py`; the terraform-specific
symbol modelling in `indexer.py` of this package.
"""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any


class LSPError(RuntimeError):
    pass


class LSPClient:
    """Drives one language-server subprocess over stdio JSON-RPC."""

    def __init__(self, server_cmd: list[str], root: Path, *,
                 timeout_s: float = 60.0) -> None:
        self._cmd = server_cmd
        self._root = root.resolve()
        self._timeout_s = timeout_s
        self._proc: subprocess.Popen[bytes] | None = None
        self._next_id = 0
        self._lock = threading.Lock()

    # --- lifecycle ---------------------------------------------------------
    def __enter__(self) -> LSPClient:
        self._proc = subprocess.Popen(
            self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        self._initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self._proc and self._proc.poll() is None:
                # best-effort graceful shutdown; don't hang on a wedged server
                try:
                    self._request("shutdown", None, timeout_s=5.0)
                    self._notify("exit", None)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        finally:
            if self._proc:
                self._proc.kill()

    # --- framing -----------------------------------------------------------
    def _write(self, msg: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + body)
        self._proc.stdin.flush()

    def _read_message(self) -> dict[str, Any]:
        """Read one LSP message (header block + JSON body). Raises on EOF."""
        assert self._proc and self._proc.stdout
        headers: dict[str, str] = {}
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise LSPError("language server closed the connection (EOF)")
            line = line.decode("ascii", "replace").strip()
            if line == "":
                break
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", "0"))
        # stdout is a pipe: a single read(n) is NOT guaranteed to return n
        # bytes — the kernel returns whatever is buffered (often ~64 KiB),
        # so a large documentSymbol response arrives truncated and
        # json.loads dies with "Unterminated string". Loop until the full
        # Content-Length body is collected (or the server closes early).
        chunks: list[bytes] = []
        remaining = n
        while remaining > 0:
            chunk = self._proc.stdout.read(remaining)
            if not chunk:
                raise LSPError(
                    f"language server closed mid-body "
                    f"({n - remaining}/{n} bytes read)"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return json.loads(b"".join(chunks).decode("utf-8"))

    # --- JSON-RPC ----------------------------------------------------------
    def _request(self, method: str, params: Any,
                 *, timeout_s: float | None = None) -> Any:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        self._write({"jsonrpc": "2.0", "id": rid, "method": method,
                     "params": params})
        # Drain messages until our matching response id arrives. Server
        # notifications / server-to-client requests interleave; skip them.
        deadline_reads = 0
        while True:
            msg = self._read_message()
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise LSPError(f"{method} → {msg['error']}")
                return msg.get("result")
            # A server-initiated REQUEST (has id + method) we must not leave
            # unanswered if it blocks the server. Reply with a null result to
            # anything we didn't register for, so the server proceeds.
            if "method" in msg and "id" in msg:
                self._write({"jsonrpc": "2.0", "id": msg["id"], "result": None})
            deadline_reads += 1
            if deadline_reads > 100000:  # runaway guard
                raise LSPError(f"{method}: no response after 100k messages")

    def _notify(self, method: str, params: Any) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _initialize(self) -> None:
        root_uri = self._root.as_uri()
        self._request("initialize", {
            "processId": None,
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": self._root.name}],
            "capabilities": {
                "textDocument": {
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "definition": {}, "references": {},
                },
                "workspace": {"symbol": {}, "workspaceFolders": True},
            },
        })
        self._notify("initialized", {})

    # --- high-level LSP methods used by the indexer ------------------------
    def did_open(self, path: Path, text: str, language_id: str = "terraform") -> None:
        self._notify("textDocument/didOpen", {"textDocument": {
            "uri": path.resolve().as_uri(), "languageId": language_id,
            "version": 1, "text": text}})

    def document_symbol(self, path: Path) -> list[dict[str, Any]]:
        res = self._request("textDocument/documentSymbol",
                            {"textDocument": {"uri": path.resolve().as_uri()}})
        return res or []

    def references(self, path: Path, line: int, char: int,
                   include_declaration: bool = False) -> list[dict[str, Any]]:
        res = self._request("textDocument/references", {
            "textDocument": {"uri": path.resolve().as_uri()},
            "position": {"line": line, "character": char},
            "context": {"includeDeclaration": include_declaration}})
        return res or []
