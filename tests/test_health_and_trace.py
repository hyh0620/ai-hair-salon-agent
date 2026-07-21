from fastapi.testclient import TestClient


def test_health_contract_and_trace_header(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_MCP_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'health.db'}")

    from app import create_app

    with TestClient(create_app()) as client:
        response = client.get("/health", headers={"X-Trace-ID": "trace-health-test"})

    assert response.status_code == 200
    assert response.headers["X-Trace-ID"] == "trace-health-test"
    body = response.json()
    assert body["app"] == "healthy"
    assert body["database"] == "healthy"
    assert body["mcp_rag"] == "unavailable"
    assert body["rag_collection"] == "unavailable"
    assert body["llm"] in {"configured", "not_configured"}
    assert body["auth"] in {"configured", "not_configured", "disabled"}
    assert body["weather"] in {"configured", "disabled"}
    assert body["weather_provider"] == "open_meteo"
    assert body["weather_location"] == "上海"
