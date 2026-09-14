"""Run inside the try container on a fresh copy of the office's records: take out everything that signs in or reaches a
mailbox, so the judges' copy shows the projects but cannot send, read mail or sign in as anyone. Prints counts only."""
import sqlite3

db = sqlite3.connect("/data/closeout.db")
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
for table in ("mail_accounts", "users", "user_links", "sessions", "issues"):
    db.execute(f"DELETE FROM {table}")
db.execute("DELETE FROM office_settings WHERE key LIKE '%client%' OR key LIKE '%secret%' OR key LIKE '%token%' OR key LIKE '%password%'")
db.commit()
db.execute("VACUUM")
ok = db.execute("PRAGMA integrity_check").fetchone()[0]
left = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("mail_accounts", "users", "sessions")}
kept = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("projects", "sheets", "documents", "reviews", "deficiencies")}
keys = [r[0] for r in db.execute("SELECT key FROM office_settings")]
print("records check:", ok)
print("sign-in and mailbox rows left:", left)
print("kept:", kept)
print("office settings keys left:", keys)
