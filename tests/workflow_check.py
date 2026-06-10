"""Verify the workflows feature in the UI."""
import re
import requests

r = requests.get("http://127.0.0.1:8000/")
html = r.text
print(f"HTML size: {len(html)} bytes")

# 1) 6 个工作流按钮
wf_ids = re.findall(r'data-wf="(\w+)"', html)
print(f"Workflows found: {wf_ids}")
assert len(wf_ids) == 6, f"Expected 6, got {len(wf_ids)}"

expected = ["ecom", "knowledge", "vlog", "clean", "quick", "tts"]
for e in expected:
    assert e in wf_ids, f"Missing {e}"
print("OK 6 workflows: ecom, knowledge, vlog, clean, quick, tts")

# 2) WORKFLOWS JS 数据
js = html[html.find('<script>'):]
for wf in expected:
    assert f"{wf}:" in js, f"Missing JS data for {wf}"
print("OK JS data present for all workflows")

# 3) 关键参数
assert "pause_threshold" in js and "min_cut_interval" in js
assert "broll_duration" in js and "add_subtitles" in js and "skip_asr" in js
print("OK all parameter keys present")

# 4) applyWorkflow / clearWorkflow
assert "function applyWorkflow" in js
assert "function clearWorkflow" in js
assert "activeWorkflow" in js
print("OK apply/clear workflow functions present")

# 5) CSS
assert ".workflows" in html
assert ".wf.active" in html
assert ".wf-tag" in html
assert ".num.star" in html
print("OK workflow CSS present")

# 6) 端到端：套用一个工作流对应的参数，跑任务
print("\n--- e2e with workflow params ---")
import os, time
with open("inputs/test_main.mp4", "rb") as f:
    r = requests.post("http://127.0.0.1:8000/api/upload-main", files={"file": f})
main_id = r.json()["file_id"]

broll_files = []
for fn in sorted(os.listdir("inputs/broll")):
    if fn.lower().endswith(".mp4"):
        broll_files.append(("files", (fn, open(f"inputs/broll/{fn}", "rb"), "video/mp4")))
r = requests.post("http://127.0.0.1:8000/api/upload-broll", files=broll_files)
broll_ids = r.json()["file_ids"]

# 模拟"带货口播"工作流的参数
body = {
    "name": "workflow_ecom_test",
    "main_file_id": main_id,
    "broll_file_ids": broll_ids,
    "options": {
        "pause_threshold": 0.5,
        "min_cut_interval": 1.5,
        "max_cuts": None,
        "broll_duration": 1.8,
        "add_subtitles": False,
        "skip_asr": True,  # skip ASR to keep test fast
    },
}
r = requests.post("http://127.0.0.1:8000/api/jobs", json=body)
print(f"create: {r.status_code} {r.json()}")
job_id = r.json()["job_id"]

for i in range(20):
    r = requests.get(f"http://127.0.0.1:8000/api/jobs/{job_id}")
    j = r.json()
    if j["status"] in ("success", "failed"):
        print(f"  FINAL: {j['status']} · draft={j.get('draft_path')}")
        break
    time.sleep(0.5)

requests.delete(f"http://127.0.0.1:8000/api/jobs/{job_id}")
print("DONE")
