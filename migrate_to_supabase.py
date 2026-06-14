import os
import sqlite3
import psycopg2

def load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()

load_env()

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "classvoice.db")
DATABASE_URL = os.environ.get("DATABASE_URL")

def migrate():
    if not DATABASE_URL:
        print("Error: DATABASE_URL environment variable is not set in .env or system env.")
        return

    if not os.path.exists(DB_PATH):
        print(f"Error: SQLite database not found at {DB_PATH}. Nothing to migrate.")
        return

    print("Connecting to databases...")
    sqlite_conn = sqlite3.connect(DB_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()

    pg_conn = psycopg2.connect(DATABASE_URL)
    pg_cursor = pg_conn.cursor()

    print("Creating tables on Supabase if not exists...")
    pg_cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id        SERIAL PRIMARY KEY,
        name      TEXT NOT NULL,
        email     TEXT NOT NULL UNIQUE,
        password  TEXT NOT NULL,
        roll_no   TEXT,
        is_admin  INTEGER NOT NULL DEFAULT 0,
        is_muted  INTEGER NOT NULL DEFAULT 0,
        is_banned INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS')
    );

    CREATE TABLE IF NOT EXISTS posts (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        message     TEXT NOT NULL,
        filename    TEXT,
        orig_name   TEXT,
        created_at  TEXT NOT NULL DEFAULT TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS'),
        deleted     INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS admin_log (
        id         SERIAL PRIMARY KEY,
        admin_id   INTEGER NOT NULL REFERENCES users(id),
        action     TEXT NOT NULL,
        target_id  INTEGER,
        note       TEXT,
        ts         TEXT NOT NULL DEFAULT TO_CHAR(NOW(), 'YYYY-MM-DD HH24:MI:SS')
    );

    CREATE TABLE IF NOT EXISTS otp_verifications (
        email       TEXT PRIMARY KEY,
        otp_hash    TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        attempts    INTEGER NOT NULL DEFAULT 0,
        last_sent   TEXT NOT NULL
    );
    """)
    pg_conn.commit()

    # Disable constraints temporarily by using truncation or importing in dependency order
    # Dependency order: users -> posts / admin_log -> otp_verifications

    print("Migrating users...")
    sqlite_cursor.execute("SELECT * FROM users")
    users = sqlite_cursor.fetchall()
    for user in users:
        pg_cursor.execute(
            "INSERT INTO users (id, name, email, password, roll_no, is_admin, is_muted, is_banned, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT(email) DO NOTHING",
            (user["id"], user["name"], user["email"], user["password"], user["roll_no"], 
             user["is_admin"], user["is_muted"], user["is_banned"], user["created_at"])
        )
    print(f"Migrated {len(users)} users.")

    print("Migrating posts...")
    sqlite_cursor.execute("SELECT * FROM posts")
    posts = sqlite_cursor.fetchall()
    for post in posts:
        pg_cursor.execute(
            "INSERT INTO posts (id, user_id, message, filename, orig_name, created_at, deleted) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT(id) DO NOTHING",
            (post["id"], post["user_id"], post["message"], post["filename"], post["orig_name"], 
             post["created_at"], post["deleted"])
        )
    print(f"Migrated {len(posts)} posts.")

    print("Migrating admin logs...")
    sqlite_cursor.execute("SELECT * FROM admin_log")
    logs = sqlite_cursor.fetchall()
    for log in logs:
        pg_cursor.execute(
            "INSERT INTO admin_log (id, admin_id, action, target_id, note, ts) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT(id) DO NOTHING",
            (log["id"], log["admin_id"], log["action"], log["target_id"], log["note"], log["ts"])
        )
    print(f"Migrated {len(logs)} admin logs.")

    print("Migrating OTP verifications...")
    sqlite_cursor.execute("SELECT * FROM otp_verifications")
    otps = sqlite_cursor.fetchall()
    for otp in otps:
        pg_cursor.execute(
            "INSERT INTO otp_verifications (email, otp_hash, expires_at, attempts, last_sent) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT(email) DO NOTHING",
            (otp["email"], otp["otp_hash"], otp["expires_at"], otp["attempts"], otp["last_sent"])
        )
    print(f"Migrated {len(otps)} OTP records.")

    print("Resetting primary key serial sequences...")
    pg_cursor.execute("SELECT setval('users_id_seq', COALESCE((SELECT MAX(id)+1 FROM users), 1), false)")
    pg_cursor.execute("SELECT setval('posts_id_seq', COALESCE((SELECT MAX(id)+1 FROM posts), 1), false)")
    pg_cursor.execute("SELECT setval('admin_log_id_seq', COALESCE((SELECT MAX(id)+1 FROM admin_log), 1), false)")

    pg_conn.commit()
    print("Migration finished successfully!")

    sqlite_conn.close()
    pg_conn.close()

if __name__ == "__main__":
    migrate()
