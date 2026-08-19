"""Event-flow resolution: turn a shared message TYPE into a cross-service
producer→consumer view.

Why this exists
---------------
SCIP resolves *static* symbol edges (imports, calls, type usage). A message-bus
hop — NATS/JetStream, Kafka, cloud pub/sub — is a *runtime* contract: a producer
publishes to a subject/topic string and a consumer subscribes to the same string,
with NO code edge between them. SCIP is structurally blind to that hop, and so is
any pure code-graph query.

BUT: in a statically-typed event system the producer constructs a message of some
type T and the consumer deserialises into the same type T — and T is (almost
always) defined in a SHARED library both import. That shared type IS a code
symbol, so ``find_references(T)`` already resolves across every producer and
consumer repo (this is the same cross-repo resolution the code graph provides for
any shared symbol). The message type is the reliable join key; the subject STRING
is not — at real call sites it is typically a bound variable or config field, not
a literal, so joining on the subject value would need dataflow analysis. Joining
on the type sidesteps that entirely.

So this module does NOT invent a new index. It takes the references the graph
already resolves for a message type and *classifies* each site as a likely
producer or consumer, grouped by repo, so the agent sees the cross-service event
flow instead of an undifferentiated reference list.

Generalisable, config-driven
-----------------------------
The mechanism (type is the join key; classify refs into produce/consume) is
generic to any typed-message system. Only the RECOGNISERS — the tokens that mark
a produce vs a consume site — are stack-specific. Defaults cover the common
cases (protobuf marshal/unmarshal, generic publish/subscribe/emit/handle verbs);
a deployment overrides them via ``STRIX_EVENT_RECOGNIZERS`` (JSON) without
touching code, mirroring the ``STRIX_CODE_GRAPH_LANGS`` selector. This keeps the
tool a generic capability with narrow, swappable, per-shop configuration.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

# See bootstrap.py's comment: renamed to a "strix."-prefixed child so
# Strix's own logging setup actually attaches a handler to it.
logger = logging.getLogger(__name__.replace("strix_code_graph", "strix.code_graph", 1))


@dataclass(frozen=True)
class Recognizers:
    """Tokens/paths that mark a reference site as a producer or a consumer.

    Matching is deliberately cheap and signal-only: we match against the
    reference's file path and the symbol descriptor we already have in the index
    (source text is NOT stored by scip expt-convert, and the query tools run
    runner-side where the in-sandbox source tree isn't readable). So this yields
    a LIKELY role, and the tool tells the agent to confirm at the site — it does
    not over-claim a definitive producer/consumer label.
    """

    produce: tuple[str, ...] = (
        # protobuf / serialisation on the way OUT
        "marshal", "encode", "serialize",
        # generic publish verbs across buses (NATS/Kafka/cloud pub-sub)
        "publish", "emit", "produce", "dispatch", "send", "enqueue",
    )
    consume: tuple[str, ...] = (
        "unmarshal", "decode", "deserialize",
        "subscribe", "consume", "handle", "handler", "onmessage",
        "process", "receive", "listener", "callback",
    )
    # Path fragments are a secondary, weaker signal (e.g. .../publisher/... vs
    # .../consumer/... , .../handlers/...). Kept small and lower-priority.
    produce_paths: tuple[str, ...] = ("publisher", "producer", "/emit")
    # NB: "worker" deliberately excluded — a worker dir both produces and
    # consumes, so it's not a reliable directional signal.
    consume_paths: tuple[str, ...] = ("consumer", "subscriber", "handler", "listener")
    # Definition-site markers to EXCLUDE from producer/consumer counts (the type
    # declaration itself, not a use). scip-go emits generated protobuf here.
    definition_markers: tuple[str, ...] = (".pb.go", ".pb.", "_pb2.", ".proto")
    extra: dict = field(default_factory=dict)


_ENV = "STRIX_EVENT_RECOGNIZERS"


def load_recognizers() -> Recognizers:
    """Recognizers from ``$STRIX_EVENT_RECOGNIZERS`` (JSON) merged over defaults.

    The env value is a JSON object with any of the keys ``produce``,
    ``consume``, ``produce_paths``, ``consume_paths``, ``definition_markers``
    (each a list of lower-case substrings). Anything omitted keeps its default.
    Malformed JSON is logged and ignored (defaults stand) — a bad config must
    never break a scan. Example for a Kafka shop::

        STRIX_EVENT_RECOGNIZERS='{"produce":["producer.send"],"consume":["poll","consumerecord"]}'
    """
    raw = os.environ.get(_ENV, "").strip()
    if not raw:
        return Recognizers()
    try:
        cfg = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("%s ignored (%s); using default event recognizers", _ENV, exc)
        return Recognizers()
    if not isinstance(cfg, dict):
        logger.warning("%s must be a JSON object; using default event recognizers", _ENV)
        return Recognizers()

    def _tuple(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        v = cfg.get(key)
        if v is None:
            return default
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            logger.warning("%s.%s must be a list of strings; keeping default", _ENV, key)
            return default
        return tuple(s.lower() for s in v)

    return Recognizers(
        produce=_tuple("produce", Recognizers.produce),
        consume=_tuple("consume", Recognizers.consume),
        produce_paths=_tuple("produce_paths", Recognizers.produce_paths),
        consume_paths=_tuple("consume_paths", Recognizers.consume_paths),
        definition_markers=_tuple("definition_markers", Recognizers.definition_markers),
    )


# roles
PRODUCER = "producer"
CONSUMER = "consumer"
DEFINITION = "definition"
UNKNOWN = "unknown"


def classify_site(rel_path: str, co_symbols: list[str], rec: Recognizers) -> str:
    """Classify one reference site as producer / consumer / definition / unknown.

    Signals, strongest first:
      1. Co-occurring symbols — the other symbols the code graph records in the
         SAME chunk as this reference (from ``get_symbol_at``). A chunk that also
         mentions ``Publish``/``Marshal`` is a produce site; ``Unmarshal``/
         ``Subscribe`` is a consume site. This is the reliable runner-side signal:
         it needs no source text, just the graph's own co-mention data.
      2. File-path conventions (``/publisher/``, ``/consumer/``, ``/handlers/``) —
         a weaker fallback when the chunk symbols are inconclusive.
    A generated-definition file (``.pb.go`` etc.) is the type declaration, not a
    use, so it is neither producer nor consumer.
    """
    p = (rel_path or "").lower()
    if any(m in p for m in rec.definition_markers):
        return DEFINITION

    # 1. Co-occurring chunk symbols (primary). Count produce vs consume hits so a
    #    chunk with both (rare) resolves to the stronger side rather than to
    #    whichever we test first. Match on word-ish boundaries so a produce token
    #    can't hide inside a consume one — critically, "marshal" must NOT match
    #    inside "unmarshal" (that would make every deserialize site look like a
    #    producer). A boundary is any non-alphanumeric char or string start/end.
    blob = " ".join(co_symbols).lower()

    def _hits(tokens: tuple[str, ...]) -> int:
        return sum(1 for tok in tokens if re.search(rf"(?<![a-z0-9]){re.escape(tok)}", blob))

    prod = _hits(rec.produce)
    cons = _hits(rec.consume)
    if cons > prod:
        return CONSUMER
    if prod > cons:
        return PRODUCER

    # 2. Directory conventions (fallback).
    if any(fp in p for fp in rec.consume_paths):
        return CONSUMER
    if any(fp in p for fp in rec.produce_paths):
        return PRODUCER
    return UNKNOWN


def repo_of(rel_path: str) -> str:
    """The target-label prefix the merge stamped onto each path
    (``<repo>/...``); ``<root>`` when unprefixed (single-target scan)."""
    return rel_path.split("/", 1)[0] if "/" in rel_path else "<root>"
