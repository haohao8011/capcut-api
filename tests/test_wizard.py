"""wizard 流程端到端测试：

1. admin 登录
2. admin 生成 setup_code（6 位人类可读）
3. 模拟员工"兑换"：调 /api/clients/wizard/redeem
4. 验证返回的明文 token 是 cap_ 开头
5. 用这个 token 发心跳 → 200
6. 同一码再兑 → 410 (已用)
7. 错码 → 404
8. admin 列 setup_codes → 应看到刚才那个
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP_DB = Path(tempfile.gettempdir()) / "capcut_test_wizard.db"
TMP_DB.unlink(missing_ok=True)
os.environ["CAPCUT_DB_URL"] = f"sqlite:///{TMP_DB}"
os.environ["CAPCUT_JWT_SECRET"] = "test_secret_for_unit_tests_only"

sys.path.insert(0, str(ROOT / "src"))

from capcut_draft import auth as auth_mod, db_models  # noqa: E402
from capcut_draft.web import app  # noqa: E402

db_models.init_all_tables()
auth_mod.seed_admin()

print("=" * 60)
print("  wizard 流程测试")
print("=" * 60)

from fastapi.testclient import TestClient
c = TestClient(app)


def step(n, name):
    print(f"\n  [{n}] {name}")


# -------- 1. admin 登录 --------
step(1, "admin 登录")
r = c.post("/api/auth/login", json={"username": "xiaoma", "password": "niubi666"})
assert r.status_code == 200, r.text
admin_h = {"Authorization": f"Bearer {r.json()['access_token']}"}
print(f"      OK")


# -------- 2. 生成 setup_code --------
step(2, "生成安装码")
r = c.post("/api/clients/wizard/setup",
           json={"name_hint": "小马的剪辑机", "ttl_minutes": 60},
           headers=admin_h)
assert r.status_code == 200, r.text
sc = r.json()
code = sc["code"]
print(f"      OK · code={code} (6 位) hint={sc['name_hint']}")
assert len(code) == 6
assert code.isalnum()


# -------- 3. 列 setup_codes --------
step(3, "admin 列安装码")
r = c.get("/api/clients/wizard/codes", headers=admin_h)
assert r.status_code == 200
codes = r.json()["codes"]
assert any(x["code"] == code for x in codes)
print(f"      OK · 总数 {len(codes)}")


# -------- 4. 员工兑换（不需要鉴权） --------
step(4, "员工兑换安装码")
r = c.post("/api/clients/wizard/redeem",
           json={"code": code, "name": "小马的剪辑机", "hostname": "TEST-PC-01", "version": "0.1.0-test"})
assert r.status_code == 200, r.text
data = r.json()
client_token = data["token"]
client_id = data["client"]["id"]
assert client_token.startswith("cap_")
print(f"      OK · client_id={client_id} token={client_token[:16]}...")


# -------- 5. 用新 token 发心跳 --------
step(5, "用换到的 token 发心跳")
h = {"Authorization": f"Bearer {client_token}"}
r = c.post("/api/clients/heartbeat", json={"is_online": True, "version": "0.1.0-test"}, headers=h)
assert r.status_code == 200, r.text
print(f"      OK · {r.json()}")


# -------- 6. 同一码再兑 → 410 --------
step(6, "同一码再兑 → 410")
r = c.post("/api/clients/wizard/redeem",
           json={"code": code, "name": "另一台", "hostname": "FAKE"})
assert r.status_code == 410, f"应该 410，实际 {r.status_code}"
print(f"      OK · 410（码已用）")


# -------- 7. 错码 → 404 --------
step(7, "错码 → 404")
r = c.post("/api/clients/wizard/redeem",
           json={"code": "ZZZZZZ", "name": "假", "hostname": "FAKE"})
assert r.status_code == 404, f"应该 404，实际 {r.status_code}"
print(f"      OK · 404（码不存在）")


# -------- 8. 生成一个过期的码 → 410 expired --------
step(8, "过期码 → 410")
r = c.post("/api/clients/wizard/setup",
           json={"name_hint": "临时", "ttl_minutes": 5},
           headers=admin_h)
expired_code = r.json()["code"]
# 直接改 expires_at 为过去（用 SQLAlchemy 模拟过期）
from datetime import datetime, timedelta, timezone
from sqlalchemy import update as _upd
with auth_mod.SessionLocal() as db:
    db.execute(
        _upd(db_models.SetupCode)
        .where(db_models.SetupCode.code == expired_code)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    db.commit()
r = c.post("/api/clients/wizard/redeem",
           json={"code": expired_code, "name": "测试", "hostname": "FAKE"})
assert r.status_code == 410, f"应该 410，实际 {r.status_code} {r.text}"
print(f"      OK · 410（码过期）")


# -------- 9. setup_code 6 位字符规则 --------
step(9, "码字符集校验：绝不含 0/O/1/I/L")
import re
for _ in range(20):
    r = c.post("/api/clients/wizard/setup",
               json={"name_hint": "test", "ttl_minutes": 5},
               headers=admin_h)
    c2 = r.json()["code"]
    # 0/1/O/I/L 都应该被去掉
    for ch in "0O1IL":
        assert ch not in c2, f"码 {c2} 居然有 {ch}"
print(f"      OK · 20 个码都避开了易混字符")


print("\n" + "=" * 60)
print("  wizard 流程测试 全部通过")
print("=" * 60)
