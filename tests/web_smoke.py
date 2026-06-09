"""Web 服务端到端冒烟测试：上传 → 创建任务 → 轮询状态。"""
import json
import os
import time
import requests

BASE = "http://localhost:8000"


def main() -> int:
    # 1) 上传主视频
    print("1) upload-main")
    with open("inputs/test_main.mp4", "rb") as f:
        r = requests.post(f"{BASE}/api/upload-main", files={"file": f})
    print("  ", r.status_code, r.json())
    if r.status_code != 200:
        return 1
    main_id = r.json()["file_id"]

    # 2) 上传 broll
    print("2) upload-broll")
    files = []
    for fn in sorted(os.listdir("inputs/broll")):
        if fn.lower().endswith(".mp4"):
            files.append(("files", (fn, open(f"inputs/broll/{fn}", "rb"), "video/mp4")))
    r = requests.post(f"{BASE}/api/upload-broll", files=files)
    print("  ", r.status_code, r.json())
    if r.status_code != 200:
        return 1
    broll_ids = r.json()["file_ids"]

    # 3) 创建任务（跳过 ASR，节省时间）
    print("3) create-job")
    body = {
        "name": "web_smoke_test",
        "main_file_id": main_id,
        "broll_file_ids": broll_ids,
        "options": {
            "skip_asr": True,
            "broll_duration": 2.0,
            "add_subtitles": False,
        },
    }
    print("   body:", json.dumps(body, ensure_ascii=False))
    r = requests.post(f"{BASE}/api/jobs", json=body)
    print("  ", r.status_code, r.text[:500])
    if r.status_code != 201:
        return 1
    job_id = r.json().get("job_id")

    # 4) 轮询状态
    if job_id:
        for i in range(60):
            r = requests.get(f"{BASE}/api/jobs/{job_id}")
            j = r.json()
            print(f"  [{i:02d}] status={j['status']}  progress_lines={len(j['progress'])}")
            if j["status"] in ("success", "failed"):
                print("   FINAL:", json.dumps(
                    {k: v for k, v in j.items() if k != "progress"},
                    ensure_ascii=False, indent=2))
                print("   tail log:")
                for line in j["progress"][-10:]:
                    print("     ", line)
                return 0 if j["status"] == "success" else 1
            time.sleep(2)
        print("超时")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
