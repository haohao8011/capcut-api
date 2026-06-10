"""客户端 API 端到端测试。

测试流程：
1. 用 admin 登录拿 access_token
2. 注册一个 client → 拿明文 token
3. 列出 clients → 应该看到刚才那个
4. 用 client token 调 heartbeat → 200
5. 再用 admin 列出 clients → 应该看到 is_online=True
6. 用 client token 调 batch_upsert_assets → 200
7. 用 admin 列出 assets → 应该看到刚上报的
8. 用 admin 删 client → 200
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 准备临时 DB（用 fresh DB 避免污染既有数据）
ROOT = Path(__file__).resolve().parent.parent
TMP_DB = Path(tempfile.gettempdir()) / "capcut_test_clients.db"
TMP_DB.unlink(missing_ok=True)
os.environ["CAPCUT_DB_URL"] = f"sqlite:///{TMP_DB}"
os.environ["CAPCUT_JWT_SECRET"] = "test_secret_for_unit_tests_only"

# 把 src 加进 sys.path


from fastapi.testclient import TestClient  # noqa: E402

from capcut_draft_server import auth as auth_mod, db_models  # noqa: E402
from capcut_draft_server.web import app  # noqa: E402

# 手动建表（TestClient 不会触发 on_event("startup")）
db_models.init_all_tables()
auth_mod.seed_admin()

print("=" * 60)
print("  客户端 API 端到端测试")
print("=" * 60)

c = TestClient(app)


def step(n, name):
    print(f"\n  [{n}] {name}")


# -------- 1. admin 登录 --------
step(1, "admin 登录")
r = c.post("/api/auth/login", json={"username": "xiaoma", "password": "niubi666"})
assert r.status_code == 200, r.text
admin_token = r.json()["access_token"]
admin_h = {"Authorization": f"Bearer {admin_token}"}
print(f"      OK · access_token 长度 {len(admin_token)}")


# -------- 2. 注册 client --------
step(2, "注册新客户端")
r = c.post("/api/clients/register",
           json={"name": "测试机-A", "hostname": "TEST-PC-01"},
           headers=admin_h)
assert r.status_code == 201, r.text
client_info = r.json()
client_token = client_info["token"]
client_id = client_info["id"]
assert client_token.startswith("cap_"), "token 应该以 cap_ 开头"
print(f"      OK · id={client_id} token={client_token[:16]}...")


# -------- 3. 列出 clients（admin 视角） --------
step(3, "admin 列出 clients")
r = c.get("/api/clients", headers=admin_h)
assert r.status_code == 200
clients = r.json()["clients"]
assert any(cl["id"] == client_id for cl in clients), "刚注册的 client 应该出现在列表"
print(f"      OK · 总数 {len(clients)}")


# -------- 4. 用 client token 发心跳 --------
step(4, "client 发心跳")
client_h = {"Authorization": f"Bearer {client_token}"}
r = c.post("/api/clients/heartbeat",
           json={"is_online": True, "version": "0.1.0-test"},
           headers=client_h)
assert r.status_code == 200, r.text
print(f"      OK · {r.json()}")


# -------- 5. 再列 clients（应看到 is_online=True） --------
step(5, "验证 client 已在线")
r = c.get("/api/clients", headers=admin_h)
clients = r.json()["clients"]
target = next(cl for cl in clients if cl["id"] == client_id)
assert target["is_online"] is True, f"刚发心跳应该 online，实际 {target['is_online']}"
print(f"      OK · is_online={target['is_online']} last_seen={target['last_seen_at']}")


# -------- 6. client 上报资产（仅元数据） --------
step(6, "client 批量上报资产元数据")
items = [
    {
        "path": "D:/fake/数字人-001.mp4",
        "name": "数字人-001.mp4",
        "kind": "main",
        "size": 12345678,
        "duration": 60.5,
        "mtime": "2025-06-10T08:00:00+00:00",
    },
    {
        "path": "D:/fake/素材-001.mp4",
        "name": "素材-001.mp4",
        "kind": "broll",
        "size": 5432100,
        "duration": 5.0,
        "mtime": "2025-06-10T08:00:00+00:00",
    },
]
r = c.post("/api/assets/batch", json={"items": items}, headers=client_h)
assert r.status_code == 200, r.text
res = r.json()
print(f"      OK · inserted={res.get('inserted')} updated={res.get('updated')}")
assert res["inserted"] == 2


# -------- 7. admin 列资产（应看到刚上报的） --------
step(7, "admin 列资产")
r = c.get("/api/assets", headers=admin_h)
assets = r.json()["assets"]
assert len(assets) == 2
kinds = {a["kind"] for a in assets}
assert kinds == {"main", "broll"}, f"应该看到 main+broll，实际 {kinds}"
print(f"      OK · {len(assets)} 个资产: {kinds}")


# -------- 8. admin 删 client --------
step(8, "admin 删 client")
r = c.delete(f"/api/clients/{client_id}", headers=admin_h)
assert r.status_code == 200
print(f"      OK")


# -------- 9. 已删 client 的 token 不应再用 --------
step(9, "已删 client 的 token 应该 401")
r = c.post("/api/clients/heartbeat", json={"is_online": True}, headers=client_h)
assert r.status_code == 401, f"应该 401，实际 {r.status_code}"
print(f"      OK · 401（token 已失效）")


# -------- 10. 重置 token 流程（再注册一个测） --------
step(10, "重置 token 流程")
r = c.post("/api/clients/register", json={"name": "测试机-B", "hostname": "TEST-PC-02"}, headers=admin_h)
assert r.status_code == 201, f"register 失败: {r.status_code} {r.text}"
cid2 = r.json()["id"]
old_token = r.json()["token"]
r = c.post(f"/api/clients/{cid2}/rotate-token", headers=admin_h)
assert r.status_code == 200
new_token = r.json()["token"]
assert new_token != old_token, "新 token 应该跟旧的不同"
print(f"      OK · old={old_token[:12]}... new={new_token[:12]}...")
# 旧 token 应失效
r = c.post("/api/clients/heartbeat", json={"is_online": True},
           headers={"Authorization": f"Bearer {old_token}"})
assert r.status_code == 401
# 新 token 应能用
r = c.post("/api/clients/heartbeat", json={"is_online": True},
           headers={"Authorization": f"Bearer {new_token}"})
assert r.status_code == 200
print(f"      OK · 旧 token 失效 / 新 token 有效")


print("\n" + "=" * 60)
print("  客户端 API 测试 全部通过")
print("=" * 60)
