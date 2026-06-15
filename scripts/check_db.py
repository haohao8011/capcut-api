from sqlalchemy import text
from capcut_draft_server.auth import engine, SessionLocal

db = SessionLocal()
r = db.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
tables = [row[0] for row in r]
print("Tables:", tables)

if "uploaded_assets" in tables:
    r2 = db.execute(text("SELECT id, filename, folder_id, owner_id FROM uploaded_assets ORDER BY id DESC LIMIT 10"))
    print("Recent uploads:")
    for row in r2:
        print(f"  id={row[0]} name={row[1]} folder_id={row[2]} owner={row[3]}")
else:
    print("uploaded_assets table NOT FOUND")

if "folders" in tables:
    r3 = db.execute(text("SELECT id, name, parent_id, owner_id FROM folders"))
    print("Folders:")
    for row in r3:
        print(f"  id={row[0]} name={row[1]} parent_id={row[2]} owner_id={row[3]}")
else:
    print("folders table NOT FOUND")
