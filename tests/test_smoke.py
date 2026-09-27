"""冒烟测试：验证骨架可导入、应用可创建。"""

from fastapi.testclient import TestClient

from app.api import create_app
from app.engine import RiskEngine


def test_app_creates() -> None:
    client = TestClient(create_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_engine_constructs() -> None:
    RiskEngine()
