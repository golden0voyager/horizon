"""Tests for ``src/ai/tokens.py`` — in-memory token usage tracker.

Covers:
- ``ProviderUsage.total`` (input + output).
- ``TokenUsageSnapshot.total_tokens``.
- ``record_usage`` skips when both inputs are <=0, accumulates otherwise.
- ``record_usage`` clamps negative inputs via ``max(0, …)``.
- ``get_usage_snapshot`` aggregates across all recorded providers.
- ``reset_usage`` clears state (used to keep tests hermetic).
"""

from src.ai.tokens import (
    ProviderUsage,
    TokenUsageSnapshot,
    get_usage_snapshot,
    record_usage,
    reset_usage,
)


def setup_function(_):
    """Run before every test in this file — guarantees a clean module state."""
    reset_usage()


# ---------------------------------------------------------------------------
# ProviderUsage.total
# ---------------------------------------------------------------------------


def test_provider_usage_total_counts_input_and_output():
    usage = ProviderUsage(input_tokens=100, output_tokens=50)
    assert usage.total == 150


def test_provider_usage_total_is_zero_when_empty():
    assert ProviderUsage().total == 0


# ---------------------------------------------------------------------------
# TokenUsageSnapshot.total_tokens
# ---------------------------------------------------------------------------


def test_token_usage_snapshot_total_sums_input_and_output():
    snap = TokenUsageSnapshot(
        total_input_tokens=200, total_output_tokens=80
    )
    assert snap.total_tokens == 280


# ---------------------------------------------------------------------------
# record_usage — happy path
# ---------------------------------------------------------------------------


def test_record_usage_accumulates_into_new_provider_bucket():
    record_usage("openai", input_tokens=10, output_tokens=20)
    snap = get_usage_snapshot()
    assert snap.total_input_tokens == 10
    assert snap.total_output_tokens == 20
    assert snap.per_provider["openai"].input_tokens == 10
    assert snap.per_provider["openai"].output_tokens == 20


def test_record_usage_accumulates_into_existing_provider_bucket():
    record_usage("anthropic", input_tokens=5, output_tokens=7)
    record_usage("anthropic", input_tokens=3, output_tokens=2)
    snap = get_usage_snapshot()
    assert snap.per_provider["anthropic"].input_tokens == 8
    assert snap.per_provider["anthropic"].output_tokens == 9


def test_record_usage_keeps_per_provider_buckets_independent():
    record_usage("openai", input_tokens=10)
    record_usage("gemini", input_tokens=20)
    snap = get_usage_snapshot()
    assert set(snap.per_provider.keys()) == {"openai", "gemini"}


# ---------------------------------------------------------------------------
# record_usage — defensive branches
# ---------------------------------------------------------------------------


def test_record_usage_skips_when_both_input_and_output_are_zero_or_negative():
    # All calls below have input<=0 AND output<=0 → must short-circuit, no bucket created.
    record_usage("noop", input_tokens=0, output_tokens=0)
    record_usage("noop", input_tokens=-1, output_tokens=0)
    record_usage("noop", input_tokens=0, output_tokens=-2)
    snap = get_usage_snapshot()
    assert "noop" not in snap.per_provider
    assert snap.total_tokens == 0


def test_record_usage_accepts_negative_values_by_clampting_to_zero():
    """One positive + one negative component → not all <=0, record; negative clamps to 0."""
    record_usage("weird", input_tokens=5, output_tokens=-10)
    snap = get_usage_snapshot()
    assert "weird" in snap.per_provider
    # Negative output gets max(0, -10) = 0
    assert snap.per_provider["weird"].output_tokens == 0
    assert snap.per_provider["weird"].input_tokens == 5


# ---------------------------------------------------------------------------
# get_usage_snapshot
# ---------------------------------------------------------------------------


def test_get_usage_snapshot_returns_empty_when_no_calls_recorded():
    snap = get_usage_snapshot()
    assert snap.total_input_tokens == 0
    assert snap.total_output_tokens == 0
    assert snap.per_provider == {}


def test_get_usage_snapshot_aggregates_across_providers():
    record_usage("openai", input_tokens=100, output_tokens=50)
    record_usage("anthropic", input_tokens=200, output_tokens=80)
    record_usage("gemini", input_tokens=30, output_tokens=20)
    snap = get_usage_snapshot()
    assert snap.total_input_tokens == 330
    assert snap.total_output_tokens == 150
    assert len(snap.per_provider) == 3


def test_get_usage_snapshot_returns_new_dict_each_call():
    """The snapshot's ``per_provider`` dict is a fresh ``dict(_provider_usage)`` copy.

    The dict itself is new on every call — mutating the dict (e.g. ``del`` a key)
    must NOT affect the module-level tracker used by the next snapshot.
    """
    record_usage("openai", input_tokens=10)
    snap1 = get_usage_snapshot()
    del snap1.per_provider["openai"]  # mutate the snapshot's dict only
    snap2 = get_usage_snapshot()
    assert "openai" in snap2.per_provider
    assert snap1.per_provider is not snap2.per_provider


# ---------------------------------------------------------------------------
# reset_usage
# ---------------------------------------------------------------------------


def test_reset_usage_clears_state():
    record_usage("openai", input_tokens=10)
    record_usage("anthropic", input_tokens=5)
    reset_usage()
    snap = get_usage_snapshot()
    assert snap.total_tokens == 0
    assert snap.per_provider == {}
