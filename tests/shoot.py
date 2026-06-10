"""先创建一个用户工作流 + 2 个 success job，再截 4 张图。"""
import json
import os
import subprocess
import time
from pathlib import Path

import requests

ROOT = Path("d:/Offices/Program/Python/capcut-api").resolve()
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT = ROOT / "screenshots"
OUT.mkdir(exist_ok=True)
URL = "http://127.0.0.1:8000/"


# 0) 删干净（避免上次测试残留）
requests.delete(f"{URL}api/workflows/u_ceafa54120")  # 上次测试的（如果还在）

# 1) 创建一个用户工作流
user_wf = {
    "name": "我的抖音带货",
    "icon": "🎁",
    "description": "1s 切，2s B-roll",
    "tags": ["1.0s 切", "2.0s B-roll", "快"],
    "options": {
        "pause_threshold": 0.6,
        "min_cut_interval": 1.0,
        "max_cuts": 25,
        "broll_duration": 2.0,
        "add_subtitles": False,
        "skip_asr": True,
    },
}
r = requests.post(f"{URL}api/workflows", json=user_wf)
print(f"user workflow saved: {r.json()['id']} {r.json()['name']}")

# 2) 预创建 2 个 success job
with open(ROOT / "inputs" / "test_main.mp4", "rb") as f:
    r = requests.post(f"{URL}api/upload-main", files={"file": f})
main_id = r.json()["file_id"]
broll_files = []
for fn in sorted(os.listdir(ROOT / "inputs" / "broll")):
    if fn.lower().endswith(".mp4"):
        broll_files.append(("files", (fn, open(ROOT / "inputs" / "broll" / fn, "rb"), "video/mp4")))
r = requests.post(f"{URL}api/upload-broll", files=broll_files)
broll_ids = r.json()["file_ids"]
for nm in ["电商主推-15s", "知识科普-60s"]:
    r = requests.post(f"{URL}api/jobs", json={
        "name": nm, "main_file_id": main_id, "broll_file_ids": broll_ids,
        "options": {"skip_asr": True, "broll_duration": 2.0, "add_subtitles": False},
    })
    jid = r.json()["job_id"]
    for _ in range(30):
        if requests.get(f"{URL}api/jobs/{jid}").json()["status"] in ("success", "failed"): break
        time.sleep(0.3)
print(f"created 2 success jobs")


# 3) 截图
def shot(name: str, width: int, height: int, wait: int = 4000):
    out = OUT / f"{name}.png"
    prof = OUT / f".edge_profile_{name}"
    if prof.exists():
        import shutil
        shutil.rmtree(prof, ignore_errors=True)
    cmd = [
        EDGE, "--headless", "--no-sandbox", "--disable-gpu",
        "--hide-scrollbars", "--disable-dev-shm-usage",
        "--disable-extensions", "--disable-features=Translate,BackForwardCache",
        f"--user-data-dir={prof}",
        f"--window-size={width},{height}",
        f"--virtual-time-budget={wait}",
        f"--screenshot={out}",
        URL,
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=60, encoding="utf-8", errors="replace")
    if out.exists():
        print(f"  ok: {out.name}  {out.stat().st_size} bytes")
    else:
        print(f"  FAIL rc={r.returncode}")


shot("01_default_1400", 1400, 1300)
shot("02_full_1600", 1600, 1500)
shot("03_mobile_400", 400, 1400)
shot("04_wide_1920", 1920, 1400)

# 4) 验证用户工作流还在
r = requests.get(f"{URL}api/workflows")
data = r.json()
print(f"\nfinal workflow count: {len(data['workflows'])}")
for w in data["workflows"]:
    mark = "[B]" if w.get("builtin") else "[U]"
    print(f"  {mark} {w['icon']} {w['name']} ({w['id']})")
