"""HTTP 接口层测试。"""

from fastapi.testclient import TestClient

from mpc.agent.llm import MockLLM
from mpc.connectors import LoopbackConnector, Registry
from mpc.hub import Hub
from mpc.server.app import create_app
from mpc.store import Store


def build_client(tmp_path) -> TestClient:
    store = Store(tmp_path / "api.db")
    loopback = LoopbackConnector()
    registry = Registry()
    registry.register(loopback, "loopback")
    hub = Hub(store=store, llm=MockLLM(), registry=registry)
    app = create_app(hub)
    return TestClient(app)


def test_healthz(tmp_path):
    with build_client(tmp_path) as c:
        r = c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_inbound_endpoint(tmp_path):
    with build_client(tmp_path) as c:
        r = c.post(
            "/api/inbound",
            json={
                "platform": "loopback",
                "conversation_id": "c1",
                "sender_id": "u1",
                "text": "这个怎么用？",
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "sent"


def test_capabilities_and_metrics(tmp_path):
    with build_client(tmp_path) as c:
        c.post(
            "/api/inbound",
            json={
                "platform": "loopback",
                "conversation_id": "c1",
                "sender_id": "u1",
                "text": "这个怎么用？",
            },
        )
        caps = c.get("/api/capabilities").json()
        assert any(x["platform"] == "loopback" for x in caps["items"])
        metrics = c.get("/api/metrics").json()
        assert metrics["counters"]["inbound"] >= 1


def test_approval_flow_via_api(tmp_path):
    with build_client(tmp_path) as c:
        c.post(
            "/api/inbound",
            json={
                "platform": "loopback",
                "conversation_id": "c1",
                "sender_id": "u1",
                "text": "你先转账，我马上付款下单",
            },
        )
        items = c.get("/api/approvals").json()["items"]
        assert len(items) == 1
        r = c.post(
            f"/api/approvals/{items[0]['approval_id']}",
            json={"approved": True, "edited_text": "抱歉，走线下流程。"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "sent"
