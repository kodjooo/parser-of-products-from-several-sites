from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.state.storage import CategoryState, StateStore


def test_state_store_cycle(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    store = StateStore(db_path)
    ts = datetime.now(timezone.utc)

    record = CategoryState(
        site_name="demo",
        category_url="https://example.com/catalog/",
        last_page=2,
        last_offset=90,
        last_product_count=180,
        last_run_ts=ts,
    )
    store.upsert(record)

    loaded = store.get("demo", "https://example.com/catalog/")
    assert loaded is not None
    assert loaded.last_page == 2
    assert loaded.last_run_ts == ts

    items = list(store.iter_site_state("demo"))
    assert len(items) == 1

    store.reset_site("demo")
    assert store.get("demo", "https://example.com/catalog/") is None

    assert list(store.iter_all()) == []

    store.close()
