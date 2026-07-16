"""Tests for event-flow classification (event_flow.classify_site + config)."""

from __future__ import annotations

import pytest

from strix_code_graph.event_flow import (
    CONSUMER,
    DEFINITION,
    PRODUCER,
    UNKNOWN,
    classify_site,
    load_recognizers,
    repo_of,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRIX_EVENT_RECOGNIZERS", raising=False)


def _rec():
    return load_recognizers()


def test_producer_from_publish_marshal_cosymbols() -> None:
    role = classify_site(
        "api/internal/service/publisher.go",
        ["DepositEvent", "EventStreamer.Publish", "proto.Marshal"],
        _rec(),
    )
    assert role == PRODUCER


def test_consumer_from_unmarshal_handler_cosymbols() -> None:
    role = classify_site(
        "sdk-api/internal/consumer/handler.go",
        ["DepositEvent", "Unmarshal", "handleDeposit"],
        _rec(),
    )
    assert role == CONSUMER


def test_marshal_does_not_match_inside_unmarshal() -> None:
    # Regression: "marshal" is a substring of "unmarshal"; a pure deserialize
    # site must NOT count as a producer. Word-boundary matching guards this.
    role = classify_site("c/h.go", ["DepositEvent", "Unmarshal"], _rec())
    assert role == CONSUMER


def test_mixed_chunk_resolves_to_stronger_side() -> None:
    # Publish (1 produce) + Unmarshal + handle (2 consume) -> consumer.
    role = classify_site("x/relay.go", ["DepositEvent", "Publish", "Unmarshal", "handle"], _rec())
    assert role == CONSUMER


def test_generated_proto_is_definition_not_a_use() -> None:
    role = classify_site(
        "shared-messages/pkg/funding/v1/funding.pb.go", ["DepositEvent"], _rec()
    )
    assert role == DEFINITION


def test_path_convention_fallback_when_no_cosymbols() -> None:
    assert classify_site("svc/internal/subscriber/w.go", ["DepositEvent"], _rec()) == CONSUMER
    assert classify_site("svc/internal/publisher/p.go", ["DepositEvent"], _rec()) == PRODUCER


def test_no_signal_is_unknown() -> None:
    # "worker" is deliberately not a directional signal.
    assert classify_site("worker/jobs/x.go", ["DepositEvent"], _rec()) == UNKNOWN


def test_repo_of_prefix_and_bare() -> None:
    assert repo_of("api/internal/x.go") == "api"
    assert repo_of("bare.go") == "<root>"


def test_recognizer_env_override_merges_over_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Kafka shop swaps consume tokens; produce defaults remain.
    monkeypatch.setenv(
        "STRIX_EVENT_RECOGNIZERS", '{"consume":["poll","consumerecord"]}'
    )
    rec = load_recognizers()
    assert "poll" in rec.consume
    assert "consumerecord" in rec.consume
    assert "unmarshal" not in rec.consume  # replaced, not appended
    assert "publish" in rec.produce  # untouched default kept
    # And it actually classifies with the override:
    assert classify_site("c/x.go", ["Evt", "consumer.Poll"], rec) == CONSUMER


def test_malformed_recognizer_env_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRIX_EVENT_RECOGNIZERS", "{not valid json")
    rec = load_recognizers()
    assert "publish" in rec.produce  # defaults stand, no crash
    assert "unmarshal" in rec.consume
