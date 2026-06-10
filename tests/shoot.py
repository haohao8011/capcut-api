"""先预创建 2 个 job（一个 success 一个 running），然后用 Edge 截 4 张图。"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("d:/Offices/Program/Python/capcut-api").resolve()
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT = ROOT / "screenshots"
OUT.mkdir(exist_ok=True)
URL = "http://127.0.0.1:8000/"


# 1) 预创建任务
import requests
with open(ROOT / "inputs" / "test_main.mp4", "rb") as f:
    r = requests.post(f"{URL}api/upload-main", files={"file": f})
main_id = r.json()["file_id"]

broll_files = []
for fn in sorted(os.listdir(ROOT / "inputs" / "broll")):
    if fn.lower().endswith(".mp4"):
        broll_files.append(("files", (fn, open(ROOT / "inputs" / "broll" / fn, "rb"), "video/mp4")))
r = requests.post(f"{URL}api/upload-broll", files=broll_files)
broll_ids = r.json()["file_ids"]

# 创建一个 success 任务（跳过 ASR 跑得快）
r = requests.post(f"{URL}api/jobs", json={
    "name": "电商主推-15s",
    "main_file_id": main_id,
    "broll_file_ids": broll_ids,
    "options": {"skip_asr": True, "broll_duration": 2.0, "add_subtitles": False},
})
success_id = r.json()["job_id"]
# 等完成
for _ in range(30):
    r = requests.get(f"{URL}api/jobs/{success_id}")
    if r.json()["status"] in ("success", "failed"):
        break
    time.sleep(0.5)
print(f"success job: {success_id} -> {r.json()['status']}")

# 创建第二个（也用成功状态以确保截图好看）
r = requests.post(f"{URL}api/jobs", json={
    "name": "知识科普-60s",
    "main_file_id": main_id,
    "broll_file_ids": broll_ids,
    "options": {"skip_asr": True, "broll_duration": 3.0, "add_subtitles": True},
})
success_id2 = r.json()["job_id"]
for _ in range(30):
    r = requests.get(f"{URL}api/jobs/{success_id2}")
    if r.json()["status"] in ("success", "failed"):
        break
    time.sleep(0.5)
print(f"success job 2: {success_id2} -> {r.json()['status']}")


# 2) 截 4 张图
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
    print(f"\n--- {name} ({width}x{height}) -> {out.name}")
    r = subprocess.run(cmd, capture_output=True, timeout=60, encoding="utf-8", errors="replace")
    if out.exists():
        print(f"  ok: {out.stat().st_size} bytes")
    else:
        print(f"  FAIL rc={r.returncode}")
        if r.stderr: print(f"  err: {r.stderr[:200]}")


shot("01_default_1400", 1400, 1300)
shot("02_full_1600", 1600, 1500)
shot("03_mobile_400", 400, 1400)
shot("04_wide_1920", 1920, 1400)

print("\n=== done ===")
for p in sorted(OUT.glob("*.png")):
    print(f"  {p}  {p.stat().st_size:>8} bytes")
