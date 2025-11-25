from __future__ import annotations

from app.network.http_client_factory import HttpClientFactory


def test_http_client_factory_reuses_clients_per_proxy():
    factory = HttpClientFactory(base_kwargs={"timeout": 5})
    client_a = factory.get("http://proxy-a:8080")
    client_b = factory.get("http://proxy-a:8080")
    client_direct = factory.get(None)

    assert client_a is client_b
    assert client_a is not client_direct
    factory.close()
