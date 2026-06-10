"""鉴权 + 路由保护 单元测试（不真起服务，用 FastAPI TestClient）。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# 清掉旧 db 让 seed_admin 重新跑
DB = ROOT / "data" / "capcut.db"
if DB.exists():
    DB.unlink()

from fastapi.testclient import TestClient  # noqa: E402
from capcut_draft_server import web  # noqa: E402
from capcut_draft_server import auth as auth_mod  # noqa: E402

client = TestClient(web.app)

results = []
def check(name, ok, extra=""):
    results.append((ok, name, extra))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}{(' — ' + extra) if extra else ''}")

print("=" * 60)
print(" 鉴权 + 路由保护 单元测试")
print("=" * 60)

# 1. 启动事件已触发（TestClient 用 with 会自动跑 startup）
with client:
    # 1.1 鉴权：seed admin 后用户存在
    with auth_mod.SessionLocal() as db:
        from sqlalchemy import select
        u = db.scalar(select(auth_mod.User).where(auth_mod.User.username == "xiaoma"))
        check("默认管理员 xiaoma 已 seed", u is not None and u.is_admin)

    # 2. 公开路由：无 token 也能访问
    r = client.get("/login")
    check("GET /login 公开访问 200", r.status_code == 200)

    r = client.post("/api/auth/login", json={})  # 错数据，看是否 422
    check("login 路由确实存在", r.status_code in (200, 422))

    # 3. 受保护路由：无 token → 401
    r = client.get("/api/workflows")
    check("GET /api/workflows 无 token → 401", r.status_code == 401,
          f"got {r.status_code}")

    r = client.get("/api/jobs")
    check("GET /api/jobs 无 token → 401", r.status_code == 401)

    r = client.post("/api/jobs", json={})
    check("POST /api/jobs 无 token → 401", r.status_code == 401)

    # 4. 错误密码登录
    r = client.post("/api/auth/login", json={"username": "xiaoma", "password": "wrong"})
    check("错误密码 → 401", r.status_code == 401)

    # 5. 正确密码登录
    r = client.post("/api/auth/login", json={"username": "xiaoma", "password": "niubi666"})
    check("xiaoma/niubi666 登录 → 200", r.status_code == 200)
    body = r.json()
    check("返回 access_token", "access_token" in body and len(body["access_token"]) > 50)
    check("返回 refresh_token", "refresh_token" in body)
    check("返回 user.is_admin=True", body.get("user", {}).get("is_admin") is True)
    token = body["access_token"]
    refresh = body["refresh_token"]

    # 6. 带 token 访问受保护路由
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/api/workflows", headers=headers)
    check("GET /api/workflows 带 token → 200", r.status_code == 200)
    data = r.json()
    check("返回了 6+ 工作流", len(data.get("workflows", [])) >= 6,
          f"count={len(data.get('workflows', []))}")

    r = client.get("/api/jobs", headers=headers)
    check("GET /api/jobs 带 token → 200", r.status_code == 200)

    # 7. /api/auth/me
    r = client.get("/api/auth/me", headers=headers)
    check("GET /api/auth/me → 200", r.status_code == 200)
    me = r.json()
    check("me.username == xiaoma", me.get("username") == "xiaoma")

    # 8. 错 token
    r = client.get("/api/workflows", headers={"Authorization": "Bearer fake.token.here"})
    check("错 token → 401", r.status_code == 401)

    # 9. 用 refresh token 换新 access
    r = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    check("refresh → 200", r.status_code == 200)
    new_token = r.json().get("access_token")
    check("新 access_token 可用",
          client.get("/api/auth/me",
                     headers={"Authorization": f"Bearer {new_token}"}).status_code == 200)

    # 10. 改密
    r = client.post("/api/auth/change-password",
                    headers=headers,
                    json={"old_password": "niubi666", "new_password": "newpass123"})
    check("改密 → 200", r.status_code == 200)
    r = client.post("/api/auth/login", json={"username": "xiaoma", "password": "newpass123"})
    check("新密码能登录", r.status_code == 200)
    r = client.post("/api/auth/login", json={"username": "xiaoma", "password": "niubi666"})
    check("旧密码失效", r.status_code == 401)

    # 恢复默认密码
    r2 = client.post("/api/auth/login", json={"username": "xiaoma", "password": "newpass123"})
    r2tok = r2.json()["access_token"]
    r = client.post("/api/auth/change-password",
                    headers={"Authorization": f"Bearer {r2tok}"},
                    json={"old_password": "newpass123", "new_password": "niubi666"})
    check("密码还原 → 200", r.status_code == 200)

    # 11. 管理员：建新用户
    r = client.post("/api/auth/users",
                    headers=headers,
                    json={"username": "tester01", "password": "test123", "is_admin": False})
    check("admin 建用户 → 201", r.status_code == 201)

    r = client.get("/api/auth/users", headers=headers)
    check("admin 列用户 → 200", r.status_code == 200)
    users = r.json()["users"]
    check("用户表含 2 人", len(users) == 2, f"got {len(users)}")

    # 12. tester01 登录（非管理员）
    r = client.post("/api/auth/login", json={"username": "tester01", "password": "test123"})
    check("tester01 登录 → 200", r.status_code == 200)
    tester_token = r.json()["access_token"]
    tester_headers = {"Authorization": f"Bearer {tester_token}"}

    # 13. tester01 不能建用户（admin 限权）
    r = client.post("/api/auth/users",
                    headers=tester_headers,
                    json={"username": "evil", "password": "test123"})
    check("非 admin 建用户 → 403", r.status_code == 403)

    # 14. tester01 不能删 admin
    r = client.delete("/api/auth/users/1", headers=tester_headers)
    check("非 admin 删用户 → 403", r.status_code == 403)

    # 15. admin 删 tester01
    r = client.delete("/api/auth/users/2", headers=headers)
    check("admin 删用户 → 200", r.status_code == 200)

    # 16. 不能删自己
    r = client.delete("/api/auth/users/1", headers=headers)
    check("不能删自己 → 400", r.status_code == 400)

    # 17. 不能删内置 xiaoma（虽然 uid=1 是自己，已经被前一条拦了）
    # 18. 数据在 SQLite 里
    with auth_mod.SessionLocal() as db:
        from sqlalchemy import select
        cnt = len(db.scalars(select(auth_mod.User)).all())
        check("DB 里有用户", cnt >= 1, f"count={cnt}")

# 总结
print("=" * 60)
ok = sum(1 for r in results if r[0])
total = len(results)
print(f" 通过 {ok}/{total}")
print("=" * 60)
sys.exit(0 if ok == total else 1)
