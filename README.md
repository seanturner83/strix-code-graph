# strix-code-graph

A [Strix](https://github.com/usestrix/strix) addon that gives the scanning agent
a **SCIP code-graph** for precise, symbol-level navigation of the target — as
opt-in agent tools, with no changes to Strix core.

Instead of grepping, the agent can ask the graph:

- `code_graph_find_definition(symbol)` — where a symbol is defined
- `code_graph_find_references(symbol)` — where it's used (definition excluded by default)
- `code_graph_find_implementations(interface)` — implementors/subtypes (from SCIP relationships)
- `code_graph_list_symbols(scope)` — what's defined under a file or directory
- `code_graph_get_imports(file)` — what a file depends on
- `code_graph_get_symbol_at(file, line)` — what a line references
- `code_graph_grep(symbol)` — resolve a symbol **and** show its definition body +
  reference sites with source context, in one call — with none of grep's false
  hits from comments, strings, vendored deps, or unrelated same-named symbols.
- `code_graph_find_event_flow(message_type)` — trace a message-bus event
  (NATS/Kafka/pub-sub) across service boundaries by its shared message **type**.
  A bus hop has no code edge — producer and consumer are linked only by a runtime
  subject/topic string — but when both sides share a statically-typed message
  contract, that type *is* a code symbol the graph resolves across repos. Returns
  an approximate producer→consumer map, classified from the symbols co-located at
  each site. Configure the produce/consume recogniser tokens per stack with
  `STRIX_EVENT_RECOGNIZERS` (JSON); works for any bus given a shared typed
  contract.

Languages: TypeScript/JavaScript, Python, Go, Rust, Java (via the respective
[Sourcegraph SCIP indexers](https://docs.sourcegraph.com/code_navigation/explanations/writing_an_indexer)),
plus **Terraform/HCL** and **Kubernetes/Helm** (no upstream SCIP indexer exists
for these — provided here).

## How it fits together

The addon is three separable pieces:

1. **This Python package** — the query tools + the SCIP indexer. Registers its
   tools through Strix's public `register_agent_tools(...)` hook (the same seam
   `register_backend` uses), so it adds no patch to Strix core.
2. **A sandbox image** carrying the SCIP toolchain (`scip`, `scip-typescript`,
   `scip-go`, `scip-python`, `scip-java`, `terraform-ls`, `helm`/`kustomize`).
   The toolchain is heavy (~2 GB), so it ships as an image *derived from* the
   base Strix sandbox rather than bloating it. Select it with `STRIX_IMAGE`.
3. **A flag** — `STRIX_CODE_GRAPH=1`. Off by default: importing the package does
   nothing until you enable it, so core Strix is untouched unless you opt in.

At scan start the addon builds the index **inside the sandbox** (where the
toolchain and the target source live), then copies the single SQLite index out
to the runner, where the query tools open it read-only. This requires a
post-session-ready hook in Strix (`register_session_setup`); without it the
tools register but report "code graph unavailable" (a clean degrade) — see
[Requirements](#requirements).

## Install & enable

```bash
pip install strix-code-graph          # alongside strix-agent

export STRIX_CODE_GRAPH=1             # enable the addon
export STRIX_IMAGE=<your-scip-sandbox-image>   # image with the SCIP toolchain
strix -t ./my-repo
```

If Strix's addon auto-discovery is available, the entry point
(`strix.addons:code_graph`) loads it automatically; otherwise call
`strix_code_graph.bootstrap.register()` at startup.

## Selecting languages

By default every language the sandbox image supports is indexed. Restrict it
per scan with a comma-separated allowlist — one image serves everyone, each scan
indexes only what it needs (faster; skips toolchains you have no repos for):

```bash
export STRIX_CODE_GRAPH_LANGS=go,python,terraform   # skip the rest
```

Unknown keys are ignored with a warning; an allowlist that matches nothing falls
back to indexing all languages (a typo shouldn't silently disable the graph).

## Multiple targets

A Strix scan can carry several targets (`-t a -t b`, `--target-list`). They land
in one sandbox under `/workspace/<subdir>`, and the addon builds **one unified
index across all of them**, so the agent can navigate the whole target set in a
single query. Each target's paths are prefixed with its subdir
(`repo-b/src/app.ts`) so identically-named files across repos never collide.

Cross-repo edges resolve wherever targets **share a symbol** — most usefully, a
type or function defined in a shared library that several targets import. The
merge is version- and module-attribution-agnostic: it keys symbol identity on
the SCIP *descriptor* (the package path + symbol), so a shared type resolves
across every consumer even when they pin different library versions and even when
the indexer attributes the reference to the consuming repo's module rather than
the dep's (both are common at scale). Genuinely different types that merely share
a display name stay separate (their descriptor paths differ).

**Limitation:** what does *not* link automatically is a purely runtime contract
with no shared code symbol — e.g. two services exchanging a raw byte payload over
a subject string, or a producer and consumer in *different languages* (whose
symbols have disjoint monikers). For the typed-message case, `find_event_flow`
bridges it via the shared message type; for the untyped/cross-language case you
get co-resident per-repo graphs — precise within each repo and attributable
across them, but not a synthesised cross-service edge.

## Failure behaviour

Code-graph is an accelerator, never a dependency. If the index can't be built
(unsupported language, empty tree, missing toolchain, no session-setup hook) the
tools return "code graph not available" and the agent proceeds with its normal
`search_files` / `read_file` path. A code-graph build failure never fails a scan.

## Requirements

- `strix-agent >= 1.1.0`
- A sandbox image with the SCIP toolchain on `PATH` (piece 2 above).
- For the index to actually build, a Strix that exposes the
  `strix.runtime.session_manager.register_session_setup` hook. This is a small,
  separate upstream contribution proposed alongside this addon; until it lands
  the tools register but report "unavailable".

## Development

```bash
pip install -e '.[dev]'
ruff check strix_code_graph tests
pytest
```

## License

Apache-2.0.
