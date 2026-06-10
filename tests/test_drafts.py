"""草稿云端存储 API 端到端测试。

覆盖：
- 上传（multipart，zip）
- 列表（搜索/筛选/分页）
- quota 查询
- 下载（计数 +1）
- 硬删（DB + 磁盘）
- 分享 token（生成/公开页/确认下载/二次使用失效）
- 路径安全（path traversal 防御）
- 权限：非 owner 不能下/删别人；admin 可以
- quota 超限 → 413

所有 DB / 草稿目录都指向临时位置，不污染真实数据。
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP_DB = Path(tempfile.gettempdir()) / "capcut_test_drafts.db"
TMP_DB.unlink(missing_ok=True)
TMP_DRAFTS_DIR = Path(tempfile.gettempdir()) / "capcut_test_drafts"
if TMP_DRAFTS_DIR.exists():
    shutil.rmtree(TMP_DRAFTS_DIR)
TMP_DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

os.environ["CAPCUT_DB_URL"] = f"sqlite:///{TMP_DB}"
os.environ["CAPCUT_JWT_SECRET"] = "test_secret_for_drafts_unit_tests"
os.environ["CAPCUT_DRAFTS_DIR"] = str(TMP_DRAFTS_DIR)
# 改小一些方便测超限
os.environ["CAPCUT_DRAFT_QUOTA_MB"] = "2"  # 2MB
os.environ["CAPCUT_DRAFT_MAX_BYTES"] = str(5 * 1024 * 1024)  # 5MB 单文件


from fastapi.testclient import TestClient  # noqa: E402

from capcut_draft_server import auth as auth_mod, db_models  # noqa: E402
from capcut_draft_server.web import app  # noqa: E402

# 手动建表
db_models.init_all_tables()
auth_mod.seed_admin()


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _login(username: str, password: str) -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _make_zip(name: str = "test.zip", size: int = 1024) -> bytes:
    """造一个 .zip 文件（content + size）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("draft_content.json", "x" * size)
        zf.writestr("draft_meta_info.json", "y" * 64)
    return buf.getvalue()


def _upload(token: str, name: str = "test.zip", size: int = 1024,
             task_id: int | None = None, task_name: str | None = None):
    data = {"file": (name, _make_zip(name, size), "application/zip")}
    form = {}
    if task_id is not None:
        form["task_id"] = str(task_id)
    if task_name:
        form["task_name"] = task_name
    return c.post("/api/drafts/upload", headers=_h(token), files=data, data=form)


# ====================================================================
print("=" * 60)
print("  草稿云端存储 API 端到端测试")
print("=" * 60)

c = TestClient(app)
admin_token = _login("xiaoma", "niubi666")

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, hint: str = "") -> None:
    if ok:
        passed.append(name)
        print(f"  ✅ {name}")
    else:
        failed.append(f"{name} ({hint})")
        print(f"  ❌ {name}  ← {hint}")


# ---- 准备 1 个普通用户 ----
r = c.post("/api/auth/users", headers=_h(admin_token),
           json={"username": "alice", "password": "alice123"})
assert r.status_code in (200, 201), r.text
alice_token = _login("alice", "alice123")

r = c.post("/api/auth/users", headers=_h(admin_token),
           json={"username": "bob", "password": "bob123"})
assert r.status_code in (200, 201), r.text
bob_token = _login("bob", "bob123")


# ============================================================
# 1. quota 查询（空）
# ============================================================
print("\n[1] quota 查询（空）")
r = c.get("/api/drafts/quota", headers=_h(alice_token))
check("quota 接口返回 200", r.status_code == 200, str(r.status_code))
q = r.json()
check("quota 默认 2MB", q["quota_mb"] == 2, str(q))
check("quota 初始 used=0", q["used_mb"] == 0, str(q))
check("quota 初始 draft_count=0", q["draft_count"] == 0, str(q))


# ============================================================
# 2. 上传（alice 上传 1 个 1KB 草稿）
# ============================================================
print("\n[2] 上传草稿")
r = _upload(alice_token, name="alice_test.zip", size=512, task_name="第一个草稿")
check("alice 上传 200", r.status_code == 200, r.text)
data = r.json()
check("返回 ok=true", data.get("ok") is True, str(data))
check("返回 draft.id", isinstance(data.get("draft", {}).get("id"), int), str(data))
check("size 正确", data["draft"]["size"] > 0)
check("task_name 正确", data["draft"]["task_name"] == "第一个草稿", str(data))
check("storage_path 在临时目录下", str(TMP_DRAFTS_DIR.name) in data["draft"]["storage_path"], str(data))
alice_draft_1 = data["draft"]["id"]
alice_draft_1_filename = data["draft"]["filename"]


# ============================================================
# 3. 磁盘上真有文件
# ============================================================
print("\n[3] 磁盘文件落地")
files = list(TMP_DRAFTS_DIR.rglob("*.zip"))
check("磁盘上有 1 个 .zip", len(files) == 1, str([f.name for f in files]))
check(".zip 名字含 draft_ 前缀和时间戳",
      files[0].name.startswith("draft_") and files[0].name.endswith(".zip"),
      files[0].name)
check("在 alice 的子目录下（owner_id 数字目录）",
      files[0].parent.parent == TMP_DRAFTS_DIR and files[0].parent.name.isdigit(),
      str(files[0]))


# ============================================================
# 4. 列表 + 权限：alice 看不到 bob 的
# ============================================================
print("\n[4] 列表 + 权限隔离")
# bob 上传一个
r = _upload(bob_token, name="bob_test.zip", size=256, task_name="bob 的草稿")
assert r.status_code == 200, r.text
bob_draft = r.json()["draft"]["id"]

# alice 列表：只看自己 1 个
r = c.get("/api/drafts", headers=_h(alice_token))
check("alice 列表 200", r.status_code == 200)
items = r.json()["items"]
check("alice 只看到自己 1 个", len(items) == 1 and items[0]["id"] == alice_draft_1, str(items))
check("alice 看不到 bob 的", all(i["id"] != bob_draft for i in items))

# bob 列表：只看自己 1 个
r = c.get("/api/drafts", headers=_h(bob_token))
check("bob 列表 200", r.status_code == 200)
items = r.json()["items"]
check("bob 只看到自己 1 个", len(items) == 1 and items[0]["id"] == bob_draft)

# admin 列表：看到 2 个
r = c.get("/api/drafts", headers=_h(admin_token))
items = r.json()["items"]
check("admin 列表 200", r.status_code == 200)
check("admin 看到 2 个", len(items) == 2, str([i["id"] for i in items]))


# ============================================================
# 5. 搜索 + 排序
# ============================================================
print("\n[5] 搜索 + 排序")
r = c.get("/api/drafts?q=alice", headers=_h(admin_token))
check("搜索 'alice' 200", r.status_code == 200)
items = r.json()["items"]
check("搜到 1 个（alice）", len(items) == 1 and items[0]["task_name"] == "第一个草稿")

r = c.get("/api/drafts?sort=size_asc&page_size=1", headers=_h(admin_token))
check("按 size 升序分页", r.status_code == 200)
items = r.json()["items"]
check("page_size=1 只 1 条", len(items) == 1)
check("total=2", r.json()["total"] == 2)

r = c.get("/api/drafts?sort=created_desc", headers=_h(admin_token))
check("created_desc 不报错", r.status_code == 200)

r = c.get("/api/drafts?min_size=400&max_size=600", headers=_h(admin_token))
check("按 size 区间筛选", r.status_code == 200)
items = r.json()["items"]
check("筛到 1 个 500B 左右的", len(items) == 1, str([i["size"] for i in items]))


# ============================================================
# 6. 下载（带计数 +1）
# ============================================================
print("\n[6] 下载（计数 +1）")
# alice 第一次下自己的
r = c.get(f"/api/drafts/{alice_draft_1}/download", headers=_h(alice_token))
check("alice 下载自己 200", r.status_code == 200, str(r.status_code))
check("Content-Type 是 zip", "zip" in r.headers.get("content-type", ""), r.headers.get("content-type"))
check("返回的是真 zip", r.content[:2] == b"PK", r.content[:4])

# 列表查 download_count 应该是 1
r = c.get("/api/drafts", headers=_h(alice_token))
items = r.json()["items"]
check("download_count 增加到 1", items[0]["download_count"] == 1, str(items[0]))
check("last_downloaded_at 已设", items[0]["last_downloaded_at"] is not None)


# ============================================================
# 7. 权限：alice 不能下 bob 的
# ============================================================
print("\n[7] 权限：非 owner 不能下别人")
r = c.get(f"/api/drafts/{bob_draft}/download", headers=_h(alice_token))
check("alice 下 bob 的 → 403", r.status_code == 403, str(r.status_code))

# admin 可以下 bob 的
r = c.get(f"/api/drafts/{bob_draft}/download", headers=_h(admin_token))
check("admin 下 bob 的 → 200", r.status_code == 200, str(r.status_code))


# ============================================================
# 8. 硬删
# ============================================================
print("\n[8] 硬删（DB + 磁盘）")
before_files = list(TMP_DRAFTS_DIR.rglob("*.zip"))
r = c.delete(f"/api/drafts/{bob_draft}", headers=_h(bob_token))
check("bob 删自己的 200", r.status_code == 200, r.text)
data = r.json()
check("返回 ok", data.get("ok") is True)
check("freed_bytes > 0", data.get("freed_bytes", 0) > 0)
check("quota 同步更新", data["quota"]["used_bytes"] == 0)

after_files = list(TMP_DRAFTS_DIR.rglob("*.zip"))
check("磁盘文件 -1", len(after_files) == len(before_files) - 1,
      f"before={len(before_files)} after={len(after_files)}")

# 二次删 → 404
r = c.delete(f"/api/drafts/{bob_draft}", headers=_h(bob_token))
check("二次删 → 404", r.status_code == 404)


# ============================================================
# 9. quota 超限 → 413
# ============================================================
print("\n[9] quota 超限（quota=2MB）")
# alice 已用 ~1KB，再传 2.5MB 就超
big_size = int(2.5 * 1024 * 1024)
r = _upload(alice_token, name="big.zip", size=big_size, task_name="撑爆 quota")
check("超大文件 → 413", r.status_code == 413, str(r.status_code))
check("错误信息提到 quota", "quota" in (r.json().get("detail") or "").lower()
      or "超限" in (r.json().get("detail") or ""), str(r.json()))

# 磁盘上没遗留半截
orphan = [f for f in TMP_DRAFTS_DIR.rglob("*")
          if f.is_file() and f.name.endswith(".zip") and "draft_" in f.name]
# 只剩 alice 第一个草稿 + 这次的失败品不应该有
check("失败上传没留半截文件（磁盘 .zip 数量 = 1）",
      len(orphan) == 1, str([f.name for f in orphan]))


# ============================================================
# 10. 分享：生成 token
# ============================================================
print("\n[10] 分享 token（生成 + 公开页 + 一次性）")
r = c.post(f"/api/drafts/{alice_draft_1}/share?ttl_days=1", headers=_h(alice_token))
check("分享 200", r.status_code == 200, r.text)
share = r.json()["share"]
check("返回 token", len(share["token"]) >= 32)
check("expires_at 已设", share["expires_at"] is not None)
check("used=false", share["used"] is False)
token = share["token"]


# ============================================================
# 11. 公开分享页（无 token 也能看）
# ============================================================
print("\n[11] 公开分享页（无需登录）")
r = c.get(f"/share/{token}")
check("访问分享页 200", r.status_code == 200, str(r.status_code))
check("HTML 含文件名", alice_draft_1_filename.encode() in r.content, "filename not in HTML")
check("HTML 含下载按钮", "确认下载".encode() in r.content)

# 假的 token → 404
r = c.get("/share/this_is_a_fake_token_xxxxxxxxxxxxxxxx")
check("假 token → 404", r.status_code == 404)


# ============================================================
# 12. 真正下载（用 ?confirm=1）
# ============================================================
print("\n[12] 公开下载（?confirm=1）")
r = c.get(f"/share/{token}/download?confirm=1")
check("确认下载 200", r.status_code == 200, r.text[:100])
check("是真 zip", r.content[:2] == b"PK", r.content[:4])


# ============================================================
# 13. 二次下载 → 410
# ============================================================
print("\n[13] 分享 token 一次性")
r = c.get(f"/share/{token}/download?confirm=1")
check("第二次用同 token → 410", r.status_code == 410, str(r.status_code))


# ============================================================
# 14. 分享页 now should show used
# ============================================================
print("\n[14] 分享页状态更新")
r = c.get(f"/share/{token}")
check("访问已用 token 的页 200", r.status_code == 200)
check("页面含'已使用'", "已使用" in r.text, r.text[:300])


# ============================================================
# 15. 非 owner 不能分享别人的草稿
# ============================================================
print("\n[15] 分享权限")
# 先让 alice 再上传一个
r = _upload(alice_token, name="alice_2.zip", size=128, task_name="alice 第二个")
alice_draft_2 = r.json()["draft"]["id"]

# bob 试图分享 alice 的 → 403
r = c.post(f"/api/drafts/{alice_draft_2}/share", headers=_h(bob_token))
check("bob 分享 alice 的 → 403", r.status_code == 403, str(r.status_code))


# ============================================================
# 16. 路径安全：手工改 DB 的 storage_path 为越界路径
# ============================================================
print("\n[16] 路径安全：path traversal 防御")
from capcut_draft_server.db_models import Draft as DBM
with auth_mod.SessionLocal() as db:
    bad = db.get(DBM, alice_draft_2)
    bad.storage_path = "/etc/passwd"  # 想越界
    db.commit()

r = c.get(f"/api/drafts/{alice_draft_2}/download", headers=_h(alice_token))
# 应该被纠正到安全路径：alice 子目录下 alice_2.zip，存在 → 200
check("越界路径被纠正（fall back 到 owner 子目录，不报 500/泄漏）",
      r.status_code == 200, str(r.status_code))


# ============================================================
# 17. 未登录 → 401
# ============================================================
print("\n[17] 未登录访问")
r = c.get("/api/drafts")
check("未登录 → 401", r.status_code == 401, str(r.status_code))

r = c.get("/api/drafts/quota")
check("未登录 quota → 401", r.status_code == 401)


# ============================================================
# 18. 上传非 .zip → 400
# ============================================================
print("\n[18] 上传非 .zip 拒绝")
r = c.post("/api/drafts/upload", headers=_h(alice_token),
           files={"file": ("test.txt", b"hello", "text/plain")})
check("非 .zip → 400", r.status_code == 400, r.text)


# ============================================================
# 19. 用户 quota 可独立覆盖
# ============================================================
print("\n[19] user.quota_mb 覆盖默认值")
with auth_mod.SessionLocal() as db:
    u = db.scalar(__import__("sqlalchemy").select(auth_mod.User).where(auth_mod.User.username == "bob"))
    u.quota_mb = 0  # 不限
    db.commit()
r = c.get("/api/drafts/quota", headers=_h(bob_token))
q = r.json()
check("bob 设了 quota=0（不限）", q["unlimited"] is True, str(q))


# ============================================================
print("\n" + "=" * 60)
total = len(passed) + len(failed)
print(f"  草稿云端测试: {len(passed)}/{total} passed")
if failed:
    print("  ❌ 失败:")
    for f in failed:
        print(f"    - {f}")
print("=" * 60)

sys.exit(0 if not failed else 1)
