from __future__ import annotations

from pathlib import Path

from app.crawler.engines import ProxyPool


def test_proxy_pool_marks_after_two_issues(tmp_path: Path) -> None:
    log_path = tmp_path / "bad.log"
    pool = ProxyPool(["http://proxy1"], bad_log_path=log_path)

    assert pool.register_issue("http://proxy1", reason="empty_page") is False
    assert not log_path.exists()

    assert pool.register_issue("http://proxy1", reason="empty_page") is True
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "empty_page" in content


def test_proxy_pool_reset_issue_counter(tmp_path: Path) -> None:
    pool = ProxyPool(["http://proxy1"])
    pool.register_issue("http://proxy1", reason="empty_page")
    pool.reset_issue_counter("http://proxy1")

    # после сброса счётчик обнуляется и прокси не блокируется при следующем единичном инциденте
    assert pool.register_issue("http://proxy1", reason="empty_page") is False


def test_proxy_pool_consecutive_errors_reset() -> None:
    pool = ProxyPool(["http://proxy1"])
    assert pool.increment_consecutive_error("http://proxy1", "ERR_TEST") == 1
    assert pool.increment_consecutive_error("http://proxy1", "ERR_TEST") == 2
    pool.reset_issue_counter("http://proxy1")
    assert pool.increment_consecutive_error("http://proxy1", "ERR_TEST") == 1


def test_proxy_pool_snapshot_counts() -> None:
    pool = ProxyPool(["http://proxy1", "http://proxy2"], allow_direct=True)
    pool.mark_bad("http://proxy1", reason="manual", log=False)
    snapshot = pool.pool_snapshot()
    assert snapshot["configured_proxies"] == 2
    assert snapshot["total_sources"] == 3
    assert snapshot["bad_proxies"] == 1
    assert snapshot["allow_direct"] is True


def test_proxy_pool_reuses_direct_connection_when_excluded() -> None:
    pool = ProxyPool([], allow_direct=True)
    first_pick = pool.pick()
    assert first_pick is None

    # даже если direct уже в exclude, повторный вызов не падает и возвращает доступный источник
    second_pick = pool.pick(exclude={None})
    assert second_pick is None
