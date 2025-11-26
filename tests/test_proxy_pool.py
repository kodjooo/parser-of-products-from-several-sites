from __future__ import annotations

import pytest

from app.crawler.engines import ProxyPool, ProxyExhaustedError


def test_proxy_pool_marks_proxy_after_two_forbidden(tmp_path):
    log_path = tmp_path / "bad.log"
    pool = ProxyPool(
        ["http://proxy.local:8080"],
        bad_log_path=log_path,
        allow_direct=False,
    )

    pool.mark_forbidden("http://proxy.local:8080")
    assert not log_path.exists()

    pool.mark_forbidden("http://proxy.local:8080")
    assert log_path.exists()
    content = log_path.read_text()
    assert "http://proxy.local:8080" in content

    with pytest.raises(ProxyExhaustedError):
        pool.pick()


def test_proxy_pool_blocks_direct_after_two_forbidden(tmp_path):
    log_path = tmp_path / "bad.log"
    pool = ProxyPool([], allow_direct=True, bad_log_path=log_path)
    pool.mark_forbidden(None)
    pool.mark_forbidden(None)

    with pytest.raises(ProxyExhaustedError):
        pool.pick()

    assert "__direct__" in log_path.read_text()


def test_proxy_pool_pick_respects_exclude():
    pool = ProxyPool(["http://proxy-a:8080", "http://proxy-b:8080"])
    choice = pool.pick(exclude={"http://proxy-a:8080"})
    assert choice == "http://proxy-b:8080"
