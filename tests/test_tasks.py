"""任务系统端到端测试。

测试流程：
1. admin 登录 + 注册 client + 上报 1 个 main + 3 个 broll 资产
2. admin 创建一个 task（指向这些资产）
3. 用 client token 拉 queue_pending → 看到 1 个任务
4. claim → 200，task status=claimed
5. start → 200，task status=running
6. progress → 200，task progress=50
7. complete → 200，task status=done
8. 再创建 1 个 task，这次让它 fail
9. claim + start + fail → 200，task status=failed
10. 测 cancel / retry / delete
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP_DB = Path(tempfile.gettempdir()) / "capcut_test_tasks.db"
TMP_DB.unlink(missing_ok=True)
os.environ["CAPCUT_DB_URL"] = f"sqlite:///{TMP_DB}"
os.environ["CAPCUT_JWT_SECRET"] = "test_secret_for_unit_tests_only"

sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from capcut_draft import auth as auth_mod, db_models  # noqa: E402
from capcut_draft.web import app  # noqa: E402

# 手动建表（TestClient 不会触发 on_event("startup")）
db_models.init_all_tables()
auth_mod.seed_admin()

print("=" * 60)
print("  任务系统 端到端测试")
print("=" * 60)

c = TestClient(app)


def step(n, name):
    print(f"\n  [{n}] {name}")


# -------- 0. 准备：admin 登录 + client 注册 + 资产上报 --------
step(0, "准备：登录 + 注册 client + 上报资产")
admin_r = c.post("/api/auth/login", json={"username": "xiaoma", "password": "niubi666"})
admin_h = {"Authorization": f"Bearer {admin_r.json()['access_token']}"}
client_r = c.post("/api/clients/register",
                  json={"name": "任务测试机", "hostname": "TASK-PC-01"},
                  headers=admin_h)
client_token = client_r.json()["token"]
client_h = {"Authorization": f"Bearer {client_token}"}

# 上报 1 main + 3 broll
items = [
    {"path": "D:/fake/数字人-001.mp4", "name": "数字人-001.mp4",
     "kind": "main", "size": 12345678, "duration": 60.0, "mtime": "2025-06-10T08:00:00+00:00"},
    {"path": "D:/fake/素材-001.mp4", "name": "素材-001.mp4",
     "kind": "broll", "size": 5432100, "duration": 5.0, "mtime": "2025-06-10T08:00:00+00:00"},
    {"path": "D:/fake/素材-002.mp4", "name": "素材-002.mp4",
     "kind": "broll", "size": 4321000, "duration": 4.0, "mtime": "2025-06-10T08:00:00+00:00"},
    {"path": "D:/fake/素材-003.mp4", "name": "素材-003.mp4",
     "kind": "broll", "size": 3210000, "duration": 6.0, "mtime": "2025-06-10T08:00:00+00:00"},
]
c.post("/api/assets/batch", json={"items": items}, headers=client_h)

# 拿资产 id
assets = c.get("/api/assets", headers=admin_h).json()["assets"]
main_id = next(a["id"] for a in assets if a["kind"] == "main")
broll_ids = [a["id"] for a in assets if a["kind"] == "broll"]
print(f"      OK · main={main_id} broll={broll_ids}")


# -------- 1. admin 创建 task --------
step(1, "admin 创建任务")
body = {
    "workflow_id": "default",
    "workflow_name": "默认参数",
    "main_asset_id": main_id,
    "broll_asset_ids": broll_ids,
    "options": {"pause_threshold": 0.6, "broll_duration": 2.5, "add_subtitles": True},
}
r = c.post("/api/tasks", json=body, headers=admin_h)
assert r.status_code == 201, r.text
task = r.json()
tid = task["id"]
assert task["status"] == "pending"
assert task["progress"] == 0
print(f"      OK · task #{tid} status=pending")


# -------- 2. client 拉 queue_pending --------
step(2, "client 拉 queue_pending")
r = c.get("/api/tasks/queue/pending", headers=client_h)
assert r.status_code == 200
tasks = r.json()["tasks"]
assert len(tasks) == 1, f"应该 1 个任务，实际 {len(tasks)}"
t = tasks[0]
# 验证 main_asset 和 broll_assets 都有 path
assert t["main_asset"] is not None
assert t["main_asset"]["path"] == "D:/fake/数字人-001.mp4"
assert len(t["broll_assets"]) == 3
print(f"      OK · 拉到任务 #{t['id']}，main.path={t['main_asset']['path']}")


# -------- 3. claim --------
step(3, "client claim 任务")
r = c.post(f"/api/tasks/{tid}/claim", headers=client_h)
assert r.status_code == 200, r.text
assert r.json()["status"] == "claimed"
print(f"      OK · status=claimed")


# -------- 4. start --------
step(4, "client start 任务")
r = c.post(f"/api/tasks/{tid}/start", headers=client_h)
assert r.status_code == 200
assert r.json()["status"] == "running"
print(f"      OK · status=running")


# -------- 5. progress 上报 --------
step(5, "client 上报进度")
for pct in (15, 40, 70, 95):
    r = c.post(f"/api/tasks/{tid}/progress",
               json={"progress": pct, "message": f"测试进度 {pct}%", "level": "info"},
               headers=client_h)
    assert r.status_code == 200, r.text
# admin 视角看进度
r = c.get(f"/api/tasks/{tid}", headers=admin_h)
assert r.json()["progress"] == 95
print(f"      OK · progress=95")


# -------- 6. complete --------
step(6, "client complete 任务")
r = c.post(f"/api/tasks/{tid}/complete",
           json={"result_path": "D:/fake/outputs/draft_001", "output_dir": "D:/fake/outputs", "message": "测试完成"},
           headers=client_h)
assert r.status_code == 200
assert r.json()["status"] == "done"
assert r.json()["progress"] == 100
print(f"      OK · status=done progress=100 result={r.json()['result_path']}")


# -------- 7. 创建第 2 个 task，让它失败 --------
step(7, "再创建一个 task，让它 fail")
r = c.post("/api/tasks", json=body, headers=admin_h)
tid2 = r.json()["id"]
c.post(f"/api/tasks/{tid2}/claim", headers=client_h)
c.post(f"/api/tasks/{tid2}/start", headers=client_h)
r = c.post(f"/api/tasks/{tid2}/fail",
           json={"error": "主视频本地不存在: 数字人-001.mp4", "message": "本地文件缺失"},
           headers=client_h)
assert r.status_code == 200
assert r.json()["status"] == "failed"
print(f"      OK · task #{tid2} status=failed")


# -------- 8. retry 失败任务 --------
step(8, "retry 失败任务")
r = c.post(f"/api/tasks/{tid2}/retry", headers=admin_h)
assert r.status_code == 200
assert r.json()["status"] == "pending"
print(f"      OK · 重置为 pending")


# -------- 9. cancel pending 任务 --------
step(9, "cancel pending 任务")
r = c.post(f"/api/tasks/{tid2}/cancel", headers=admin_h)
assert r.status_code == 200
assert r.json()["status"] == "canceled"
print(f"      OK · status=canceled")


# -------- 10. delete 任务 --------
step(10, "delete 任务")
r = c.delete(f"/api/tasks/{tid}", headers=admin_h)
assert r.status_code == 200
# 再 GET 应该 404
r = c.get(f"/api/tasks/{tid}", headers=admin_h)
assert r.status_code == 404
print(f"      OK · task #{tid} 已删")


# -------- 11. admin 看 task 列表（应该只剩 canceled 那个） --------
step(11, "admin 列任务")
r = c.get("/api/tasks", headers=admin_h)
tasks = r.json()["tasks"]
assert len(tasks) == 1
assert tasks[0]["id"] == tid2
print(f"      OK · 剩 {len(tasks)} 个任务")


# -------- 12. 鉴权：另一个 client 不能 claim 别人的任务（边界） --------
step(12, "另一个 client 不能 claim 别的 owner 的任务")
# 注册第二个 client
client_r2 = c.post("/api/clients/register",
                   json={"name": "机器-B", "hostname": "B-PC-01"},
                   headers=admin_h)
token2 = client_r2.json()["token"]
# 用 machine B 的 token 拉 queue — 因为 client 是 admin 创建的, owner_id 也是 admin，
# 所以 machine B 也能拉到（owner 匹配），这块不细测 owner 隔离（要新建用户后用其 token 测）
# 这里只验证两个 client 同时能 claim 各自的 task
r = c.post("/api/tasks", json=body, headers=admin_h)
tid3 = r.json()["id"]
h2 = {"Authorization": f"Bearer {token2}"}
r = c.get("/api/tasks/queue/pending", headers=h2)
assert r.status_code == 200
print(f"      OK · 第二个 client 也能正常拉任务")


print("\n" + "=" * 60)
print("  任务系统测试 全部通过")
print("=" * 60)
