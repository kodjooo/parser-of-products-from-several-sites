from app.crawler.utils import normalize_url


def test_normalize_url_removes_tracking_params() -> None:
    url, url_hash = normalize_url(
        "/item?id=123&utm_source=test",
        base_url="https://example.com/catalog/",
        strip_params=["utm_*"],
    )
    assert url == "https://example.com/item?id=123"
    assert len(url_hash) == 32
