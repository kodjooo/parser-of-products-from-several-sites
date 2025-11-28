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
