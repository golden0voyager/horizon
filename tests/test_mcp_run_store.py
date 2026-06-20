"""Phase 5 unit tests for ``src.mcp.run_store.RunStore``.

Covers filesystem-based run artifact persistence: ``create_run`` (idempotent
metadata bootstrapping), ``list_runs`` (sort by updated_at), ``save_items`` /
``load_items`` JSON round-trip, ``save_summary`` / ``load_summary``, meta
update with updated_at stamps, ``has_stage``, ``run_dir`` raises when missing,
STAGES validation, run_id traversal protection (``..`` rejection), and
language / run_id pattern enforcement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.mcp.run_store import RUN_ID_RE, STAGES, RunStore

# ---------------------------------------------------------------------------
# Constants and static helpers
# ---------------------------------------------------------------------------


def test_stages_contains_expected_keys() -> None:
    assert set(STAGES.keys()) == {"raw", "scored", "filtered", "enriched"}


def test_run_id_re_pattern() -> None:
    assert RUN_ID_RE.fullmatch("run-20260115T000000Z-abc12345")
    assert RUN_ID_RE.fullmatch("plain_run_id")
    assert not RUN_ID_RE.fullmatch("/etc/passwd")  # slash forbidden
    assert not RUN_ID_RE.fullmatch("")  # empty
    assert not RUN_ID_RE.fullmatch("/etc/passwd")  # slash forbidden
    assert not RUN_ID_RE.fullmatch("")  # empty
    # The regex anchors: starts-with-digit is allowed (regex doesn't restrict leading char class).
    assert RUN_ID_RE.fullmatch("1run")


def test_stage_file_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported stage"):
        RunStore._stage_file("bogus-stage")


def test_stage_file_valid() -> None:
    assert RunStore._stage_file("raw") == "raw_items.json"


def test_summary_file_invalid_language_raises() -> None:
    with pytest.raises(ValueError, match="Invalid summary language"):
        RunStore._summary_file("../escape")


def test_summary_file_valid() -> None:
    assert RunStore._summary_file("zh") == "summary-zh.md"


def test_make_run_id_format() -> None:
    rid = RunStore._make_run_id()
    assert rid.startswith("run-")
    assert RUN_ID_RE.fullmatch(rid)


def test_utc_now_iso8601() -> None:
    from datetime import datetime
    iso = RunStore._utc_now()
    parsed = datetime.fromisoformat(iso)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# Instance methods — filesystem-backed CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(root=tmp_path)


def test_init_creates_root(tmp_path: Path) -> None:
    root = tmp_path / "new-runs"
    RunStore(root=root)
    assert root.is_dir()


def test_run_path_traversal_rejected(store: RunStore) -> None:
    with pytest.raises(ValueError, match="Invalid run_id"):
        store._run_path("../escape")


def test_run_path_double_dot_rejected(store: RunStore) -> None:
    with pytest.raises(ValueError, match="Invalid run_id"):
        store._run_path("good..bad")


def test_run_path_valid(store: RunStore) -> None:
    p = store._run_path("run-01")
    assert p.name == "run-01"


def test_run_dir_missing_raises_file_not_found(store: RunStore) -> None:
    with pytest.raises(FileNotFoundError, match="Run not found"):
        store.run_dir("missing-run")


def test_create_run_default_id_creates_meta(store: RunStore) -> None:
    rid = store.create_run()
    assert RUN_ID_RE.fullmatch(rid)
    meta = store.load_meta(rid)
    assert meta["run_id"] == rid
    assert "created_at" in meta
    assert "updated_at" not in meta  # only assigned on update.


def test_create_run_explicit_id(store: RunStore) -> None:
    rid = store.create_run("my-run-001")
    assert rid == "my-run-001"
    meta = store.load_meta("my-run-001")
    assert meta["run_id"] == "my-run-001"


def test_create_run_idempotent_on_existing_meta(store: RunStore) -> None:
    """``create_run`` does NOT overwrite existing meta.json (so a re-call preserves original created_at)."""

    meta_first = store.create_run("dup-run")
    created_at_first = store.load_meta("dup-run")["created_at"]
    meta_second = store.create_run("dup-run")
    assert meta_first == meta_second  # same id returned
    assert store.load_meta("dup-run")["created_at"] == created_at_first


def test_list_runs_empty(store: RunStore) -> None:
    assert store.list_runs() == []


def test_list_runs_sorts_by_updated_desc(store: RunStore) -> None:
    store.create_run("a")
    store.create_run("b")
    store.create_run("c")
    # Touch 'a' so it jumps to top.
    store.update_meta("a", {"touched": True})

    out = store.list_runs(limit=10)
    assert [r["run_id"] for r in out] == ["a", "c", "b"]


def test_list_runs_limit(store: RunStore) -> None:
    for i in range(5):
        store.create_run(f"r{i}")
    out = store.list_runs(limit=2)
    assert len(out) == 2


def test_list_runs_skips_dirs_without_meta(store: RunStore, tmp_path: Path) -> None:
    """A directory under root without ``meta.json`` should be skipped silently."""

    store.create_run("run-with-meta")
    (tmp_path / "run-no-meta").mkdir()
    out = store.list_runs(limit=10)
    assert len(out) == 1
    assert out[0]["run_id"] == "run-with-meta"


def test_save_load_items_round_trip(store: RunStore) -> None:
    rid = store.create_run("run-items")
    items = [{"id": "1", "title": "t1"}, {"id": "2", "title": "t2"}]
    store.save_items(rid, "raw", items)
    assert store.has_stage(rid, "raw")
    assert store.load_items(rid, "raw") == items


def test_save_items_unknown_stage_raises(store: RunStore) -> None:
    rid = store.create_run("bad-stage")
    with pytest.raises(ValueError, match="Unsupported stage"):
        store.save_items(rid, "scored-with-typo", [])


def test_load_items_missing_raises(store: RunStore) -> None:
    rid = store.create_run("run-empty")
    with pytest.raises(FileNotFoundError, match="Artifact not found"):
        store.load_items(rid, "raw")


def test_save_load_summary_round_trip(store: RunStore) -> None:
    rid = store.create_run("run-sum")
    store.save_summary(rid, "zh", "# 中文日报\nbody")
    assert store.load_summary(rid, "zh") == "# 中文日报\nbody"


def test_load_summary_missing_raises(store: RunStore) -> None:
    rid = store.create_run("run-sum-missing")
    with pytest.raises(FileNotFoundError, match="Summary not found"):
        store.load_summary(rid, "en")


def test_save_summary_invalid_language_raises(store: RunStore) -> None:
    rid = store.create_run("run-sum-bad-lang")
    with pytest.raises(ValueError, match="Invalid summary language"):
        store.save_summary(rid, "../etc", "x")


def test_has_stage_false_when_missing(store: RunStore) -> None:
    rid = store.create_run("no-stage")
    assert store.has_stage(rid, "raw") is False


def test_update_meta_merges_and_stamps(store: RunStore) -> None:
    rid = store.create_run("meta-stamp")
    store.update_meta(rid, {"foo": 1, "bar": 2})
    meta = store.load_meta(rid)
    assert meta["foo"] == 1
    assert meta["bar"] == 2
    assert "updated_at" in meta
    updated1 = meta["updated_at"]

    store.update_meta(rid, {"foo": 99})
    meta2 = store.load_meta(rid)
    assert meta2["foo"] == 99
    assert meta2["bar"] == 2  # previous key preserved.
    assert meta2["updated_at"] >= updated1


def test_update_meta_missing_run_raises(store: RunStore) -> None:
    with pytest.raises(FileNotFoundError):
        store.update_meta("not-here", {"k": 1})


def test_write_json_stores_pretty(store: RunStore) -> None:
    rid = store.create_run("json-test")
    path = store.write_json(rid, "x.json", {"k": [1, 2, 3]})
    assert path.exists()
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed == {"k": [1, 2, 3]}


def test_read_json_missing_raises(store: RunStore) -> None:
    rid = store.create_run("json-missing")
    with pytest.raises(FileNotFoundError, match="Artifact not found"):
        store.read_json(rid, "ghost.json")
