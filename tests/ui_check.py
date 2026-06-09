"""Verify the new UI HTML has all the new features and bug fixes."""
import requests

r = requests.get('http://127.0.0.1:8000/')
html = r.text
print(f"Total HTML: {len(html)} bytes, {r.status_code}")

checks = [
    ("dark theme tokens", "--bg-0" in html),
    ("JetBrains Mono font", "JetBrains Mono" in html),
    ("Noto Sans SC font", "Noto Sans SC" in html),
    ("broll upload route", "kind === 'broll'" in html or 'kind === "broll"' in html),
    ("broll endpoint URL", "/api/upload-broll" in html),
    ("custom switch widget", "opt-check" in html),
    ("progress bar", "progress .bar" in html or ".progress .bar" in html),
    ("retry button", 'data-act="retry"' in html),
    ("remove file btn", "data-kind" in html),
    ("a11y aria-label", "aria-label" in html),
    ("status glyph map", "STATUS_GLYPH" in html),
    ("toast stack (not single)", "toast-stack" in html),
    ("escape html helper", "escapeHtml" in html),
    ("polling stop when idle", "stopPolling" in html),
    ("brand logo", "logo\">C<" in html or 'logo">C<' in html),
    ("grid layout", "grid-template-columns: 1.05fr" in html),
    ("4 numbered steps", 'class="num">01' in html and 'class="num">04' in html),
    ("reset button", "btn-reset" in html),
    ("responsive media query", "@media (max-width: 960px)" in html),
    ("state.main array", "state.main" in html),
]
all_ok = True
for name, ok in checks:
    if not ok: all_ok = False
    print(f"  {'OK ' if ok else 'MISS'}  {name}")
print("RESULT:", "ALL GOOD" if all_ok else "SOME MISSING")

# 也跑一下完整上传流程，确保没把 API 弄坏
print("\n--- end-to-end smoke ---")
import os, time
with open("inputs/test_main.mp4", "rb") as f:
    r = requests.post("http://127.0.0.1:8000/api/upload-main", files={"file": f})
print(f"upload-main: {r.status_code} {r.json()}")
main_id = r.json()["file_id"]

broll_files = []
for fn in sorted(os.listdir("inputs/broll")):
    if fn.lower().endswith(".mp4"):
        broll_files.append(("files", (fn, open(f"inputs/broll/{fn}", "rb"), "video/mp4")))
r = requests.post("http://127.0.0.1:8000/api/upload-broll", files=broll_files)
print(f"upload-broll: {r.status_code} {r.json()}")
broll_ids = r.json()["file_ids"]

body = {
    "name": "ui_redesign_test",
    "main_file_id": main_id,
    "broll_file_ids": broll_ids,
    "options": {"skip_asr": True, "broll_duration": 1.5, "add_subtitles": False},
}
r = requests.post("http://127.0.0.1:8000/api/jobs", json=body)
print(f"create job: {r.status_code} {r.json()}")
job_id = r.json()["job_id"]

for i in range(20):
    r = requests.get(f"http://127.0.0.1:8000/api/jobs/{job_id}")
    j = r.json()
    print(f"  [{i:02d}] {j['status']:8s} progress={len(j.get('progress', []))}")
    if j["status"] in ("success", "failed"):
        print(f"  FINAL: {j['status']}, draft={j.get('draft_path')}")
        break
    time.sleep(1)

# 清理
requests.delete(f"http://127.0.0.1:8000/api/jobs/{job_id}")
print("cleaned up")
