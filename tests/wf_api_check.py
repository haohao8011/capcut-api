"""验证工作流 API：列 / 存 / 删。"""
import json
import requests

BASE = "http://127.0.0.1:8000"

# 1) 列表（应有 6 个内置 + 0 个用户）
r = requests.get(f"{BASE}/api/workflows")
print(f"GET /api/workflows: {r.status_code}")
data = r.json()
count = len(data["workflows"])
print(f"  count: {count}")
for w in data["workflows"]:
    mark = "[B]" if w.get("builtin") else "[U]"
    print(f"    {mark} {w['icon']} {w['name']} ({w['id']})")
assert count == 6, f"expected 6 builtin, got {count}"

# 2) 存一个用户工作流
print()
wf_body = {
    "name": "我的带货 v2",
    "icon": "🎁",
    "description": "自用·抖音",
    "tags": ["1.0s 切", "2.0s B-roll"],
    "options": {
        "pause_threshold": 0.6,
        "min_cut_interval": 1.0,
        "max_cuts": 20,
        "broll_duration": 2.0,
        "add_subtitles": False,
        "skip_asr": True,
    },
}
r = requests.post(f"{BASE}/api/workflows", json=wf_body)
print(f"POST /api/workflows: {r.status_code}")
saved = r.json()
print(f"  saved: {saved['id']} name={saved['name']} builtin={saved['builtin']}")
print(f"  options: {json.dumps(saved['options'], ensure_ascii=False)}")
assert r.status_code == 201
assert saved["id"].startswith("u_")
assert saved["builtin"] is False

# 3) 再列一次（应 6 内置 + 1 用户 = 7）
r = requests.get(f"{BASE}/api/workflows")
data = r.json()
print(f"\nGET after save: {len(data['workflows'])} workflows")
assert len(data["workflows"]) == 7

# 4) 试图删一个内置的（应失败）
r = requests.delete(f"{BASE}/api/workflows/ecom")
print(f"\nDELETE builtin 'ecom': {r.status_code} {r.text[:100]}")
assert r.status_code == 400

# 5) 删用户工作流
r = requests.delete(f"{BASE}/api/workflows/{saved['id']}")
print(f"DELETE user '{saved['id']}': {r.status_code} {r.json()}")
assert r.status_code == 200

# 6) 确认删完了
r = requests.get(f"{BASE}/api/workflows")
assert len(r.json()["workflows"]) == 6
print(f"\nDONE. final count: 6 (cleaned up)")
