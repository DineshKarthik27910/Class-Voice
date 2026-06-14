import threading

import os
import uuid
import html
import urllib.parse
import random
import hashlib
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
import sqlite3
import requests
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_from_directory, abort
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO, emit, disconnect



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

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (os.environ.get("FLASK_ENV", "development") == "production")

# Use a dedicated data directory for SQLite database and uploads
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
ALLOWED_EXTENSIONS = {"pdf", "docx", "pptx", "png", "jpg", "jpeg"}
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

DB_PATH = os.path.join(DATA_DIR, "classvoice.db")

COLLEGE_DOMAIN = os.environ.get("COLLEGE_DOMAIN", "bl.students.amrita.edu")

# SMTP Configuration for OTP Verification
SMTP_SERVER = os.environ.get("SMTP_SERVER")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_EMAIL = os.environ.get("SMTP_EMAIL")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ALLOW_CONSOLE_OTP = os.environ.get("ALLOW_CONSOLE_OTP", "false").lower() == "true"
FLASK_ENV = os.environ.get("FLASK_ENV", "development")

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", manage_session=True, async_mode='threading')

# Global trackers
connected_users = {}  # user_id -> set of sids
rate_limits = {}      # user_id -> list of floats (timestamps)

# ── DB helpers ──────────────────────────────────────────────────────────────
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

class SQLiteCompatiblePostgreSQLCursor:
    def __init__(self, pg_cursor):
        self._cursor = pg_cursor
        self._lastrowid = None

    def execute(self, sql, parameters=None):
        if sql and '?' in sql:
            sql = sql.replace('?', '%s')
        
        if parameters is not None:
            self._cursor.execute(sql, parameters)
        else:
            self._cursor.execute(sql)
        
        if "returning id" in sql.lower():
            try:
                row = self._cursor.fetchone()
                if row:
                    self._lastrowid = row[0]
            except Exception:
                pass
        return self

    @property
    def lastrowid(self):
        return self._lastrowid

    def fetchone(self):
        try:
            return self._cursor.fetchone()
        except psycopg2.ProgrammingError as e:
            if "no results to fetch" in str(e).lower():
                return None
            raise

    def fetchall(self):
        try:
            return self._cursor.fetchall()
        except psycopg2.ProgrammingError as e:
            if "no results to fetch" in str(e).lower():
                return []
            raise

class SQLiteCompatiblePostgreSQLConnection:
    def __init__(self, pg_conn):
        self._conn = pg_conn

    def cursor(self):
        return SQLiteCompatiblePostgreSQLCursor(self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor))

    def execute(self, sql, parameters=None):
        cur = self.cursor()
        cur.execute(sql, parameters)
        return cur

    def executescript(self, sql_script):
        cur = self.cursor()
        cur.execute(sql_script)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is not None:
                self.rollback()
            else:
                self.commit()
        finally:
            self.close()

def get_db():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        pg_conn = psycopg2.connect(database_url)
        return SQLiteCompatiblePostgreSQLConnection(pg_conn)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    database_url = os.environ.get("DATABASE_URL")
    with get_db() as conn:
        if database_url:
            # PostgreSQL schema
            conn.executescript("""
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
            
            cursor = conn.execute("""
                SELECT column_name AS name 
                FROM information_schema.columns 
                WHERE table_name = 'users'
            """)
            columns = [row["name"] for row in cursor.fetchall()]
            if "is_muted" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN is_muted INTEGER NOT NULL DEFAULT 0")
            if "is_banned" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0")
                
            admin_exists = conn.execute("SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1").fetchone()
            if not admin_exists:
                import secrets
                temp_password = secrets.token_urlsafe(16)
                admin_hash = generate_password_hash(temp_password)
                conn.execute(
                    "INSERT INTO users (name, email, password, is_admin) VALUES (?, ?, ?, 1)",
                    ("Admin", "bl.sc.u4aie25240@bl.students.amrita.edu", admin_hash)
                )
                print(f"\n[SECURITY] Seeded initial admin account (bl.sc.u4aie25240@bl.students.amrita.edu) with password: {temp_password}\n", flush=True)
        else:
            # SQLite DDL
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL,
                email     TEXT NOT NULL UNIQUE,
                password  TEXT NOT NULL,
                roll_no   TEXT,
                is_admin  INTEGER NOT NULL DEFAULT 0,
                is_muted  INTEGER NOT NULL DEFAULT 0,
                is_banned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS posts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                message     TEXT NOT NULL,
                filename    TEXT,
                orig_name   TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                deleted     INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS admin_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id   INTEGER NOT NULL REFERENCES users(id),
                action     TEXT NOT NULL,
                target_id  INTEGER,
                note       TEXT,
                ts         TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS otp_verifications (
                email       TEXT PRIMARY KEY,
                otp_hash    TEXT NOT NULL,
                expires_at  TEXT NOT NULL,
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_sent   TEXT NOT NULL
            );
            """)
            cursor = conn.execute("PRAGMA table_info(users)")
            columns = [row["name"] for row in cursor.fetchall()]
            if "is_muted" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN is_muted INTEGER NOT NULL DEFAULT 0")
            if "is_banned" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0")

            posts = conn.execute("SELECT id, created_at FROM posts").fetchall()
            for post in posts:
                post_id = post["id"]
                orig_ts = post["created_at"]
                new_ts = orig_ts
                if " " in new_ts and "T" not in new_ts:
                    new_ts = new_ts.replace(" ", "T")
                if not new_ts.endswith("Z") and "+00:00" not in new_ts:
                    new_ts += "+00:00"
                if new_ts != orig_ts:
                    conn.execute("UPDATE posts SET created_at = ? WHERE id = ?", (new_ts, post_id))

            admin_exists = conn.execute("SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1").fetchone()
            if not admin_exists:
                import secrets
                temp_password = secrets.token_urlsafe(16)
                admin_hash = generate_password_hash(temp_password)
                conn.execute(
                    "INSERT INTO users (name, email, password, is_admin) VALUES (?, ?, ?, 1)",
                    ("Admin", "bl.sc.u4aie25240@bl.students.amrita.edu", admin_hash)
                )
                print(f"\n[SECURITY] Seeded initial admin account (bl.sc.u4aie25240@bl.students.amrita.edu) with password: {temp_password}\n", flush=True)
        conn.commit()

# Initialize directories and database at module import time (essential for production Gunicorn setup)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
init_db()

def log_admin_action(admin_id, action, target_id=None, note=None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO admin_log (admin_id, action, target_id, note) VALUES (?,?,?,?)",
            (admin_id, action, target_id, note)
        )

# ── Auth helpers ─────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        # Verify user is not banned in db
        with get_db() as conn:
            user = conn.execute("SELECT is_banned FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if user and user["is_banned"]:
            session.clear()
            flash("Your account has been banned.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session or not session.get("is_admin"):
            abort(403)
        return f(*args, **kwargs)
    return decorated

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ── Routes: Auth & OTP Verification ──────────────────────────────────────────

def send_verification_email(email, otp):
    subject = "Class Voice Verification Code"
    body = f"Your verification code for Class Voice is: {otp}\n\nThis code will expire in 10 minutes."
    
    brevo_api_key = os.environ.get("BREVO_API_KEY")
    brevo_from = os.environ.get("BREVO_FROM_EMAIL")
    
    if brevo_api_key and brevo_from:
        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "api-key": brevo_api_key,
            "Content-Type": "application/json",
            "accept": "application/json"
        }
        payload = {
            "sender": {
                "name": "Class Voice",
                "email": brevo_from
            },
            "to": [
                {
                    "email": email
                }
            ],
            "subject": subject,
            "textContent": body
        }
        try:
            print(f"[Brevo] Sending email to {email}...", flush=True)
            response = requests.post(url, json=payload, headers=headers, timeout=10.0)
            if response.status_code in (200, 201, 202):
                print(f"[Brevo] Email sent successfully to {email}", flush=True)
                return True
            else:
                print(f"[Brevo Error] Failed to send email (Status {response.status_code}): {response.text}", flush=True)
                if ALLOW_CONSOLE_OTP:
                    print(f"[CONSOLE LOG - SMTP FALLBACK] OTP for {email} is: {otp}", flush=True)
                    return True
                return False
        except Exception as e:
            print(f"[Brevo Error] Request failed: {e}", flush=True)
            if ALLOW_CONSOLE_OTP:
                print(f"[CONSOLE LOG - SMTP FALLBACK] OTP for {email} is: {otp}", flush=True)
                return True
            return False
    else:
        if FLASK_ENV == "development" or ALLOW_CONSOLE_OTP:
            print(f"[CONSOLE LOG - DEV ONLY] OTP for {email} is: {otp}", flush=True)
            return True
        else:
            print("[Brevo Error] BREVO_API_KEY or BREVO_FROM_EMAIL is not configured and console fallback is disabled.", flush=True)
            return False

@app.route("/")
def index():
    if "user_id" in session:
        with get_db() as conn:
            user = conn.execute("SELECT is_banned FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if user and user["is_banned"]:
            session.clear()
            return redirect(url_for("login"))
        return redirect(url_for("feed"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        with get_db() as conn:
            user = conn.execute("SELECT is_banned FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if user and user["is_banned"]:
            session.clear()
        else:
            return redirect(url_for("feed"))
        
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or not check_password_hash(user["password"], password):
            flash("Invalid credentials.", "error")
            return render_template("login.html")
        
        if user["is_banned"]:
            flash("Your account has been banned.", "error")
            return redirect(url_for("login"))
            
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["is_admin"] = bool(user["is_admin"])
        session.permanent = True
        print(f"[PASSWORD RESET] Successful login for: {email}", flush=True)
        return redirect(url_for("admin_dashboard") if user["is_admin"] else url_for("feed"))
        
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("feed"))
        
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        
        if not name or not email or not password:
            flash("Please fill in all fields.", "error")
            return render_template("register.html")
            
        if not email.endswith(f"@{COLLEGE_DOMAIN}"):
            flash(f"Only @{COLLEGE_DOMAIN} email addresses are allowed.", "error")
            return render_template("register.html")
            
        with get_db() as conn:
            existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            flash("Email already registered. Please log in instead.", "error")
            return redirect(url_for("login"))
            
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            otp_row = conn.execute("SELECT last_sent FROM otp_verifications WHERE email=?", (email,)).fetchone()
        if otp_row:
            last_sent = datetime.strptime(otp_row["last_sent"], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - last_sent).total_seconds() < 60:
                flash("Please wait 60 seconds before requesting another code.", "error")
                return render_template("register.html")
                
        otp = f"{random.randint(100000, 999999)}"
        otp_hash = hashlib.sha256(otp.encode("utf-8")).hexdigest()
        expires_at = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        
        with get_db() as conn:
            conn.execute("""
                INSERT INTO otp_verifications (email, otp_hash, expires_at, attempts, last_sent)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(email) DO UPDATE SET
                    otp_hash=excluded.otp_hash,
                    expires_at=excluded.expires_at,
                    attempts=0,
                    last_sent=excluded.last_sent
            """, (email, otp_hash, expires_at, now_str))
            
        session["pending_reg"] = {
            "action": "register",
            "name": name,
            "email": email,
            "password_hash": generate_password_hash(password)
        }
        if FLASK_ENV == "development":
            session["dev_otp"] = otp
        
        if send_verification_email(email, otp):
            flash("Verification code sent to your email.", "success")
            return redirect(url_for("verify_otp"))
        else:
            flash("Failed to send verification email. Please contact support or check SMTP settings.", "error")
            return render_template("register.html")
        
    return render_template("register.html")

@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    if "pending_reg" not in session:
        flash("No pending verification request found. Please start here.", "error")
        return redirect(url_for("register"))
        
    pending = session["pending_reg"]
    email = pending["email"]
    action = pending["action"]
    
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        if not code:
            flash("Please enter the verification code.", "error")
            return render_template("verify_otp.html", email=email)
            
        with get_db() as conn:
            otp_row = conn.execute("SELECT * FROM otp_verifications WHERE email=?", (email,)).fetchone()
            
        if not otp_row:
            flash("Verification session not found. Please request a new code.", "error")
            session.pop("pending_reg", None)
            return redirect(url_for("register") if action == "register" else url_for("forgot_password"))
            
        attempts = otp_row["attempts"]
        if attempts >= 3:
            with get_db() as conn:
                conn.execute("DELETE FROM otp_verifications WHERE email=?", (email,))
            session.pop("pending_reg", None)
            flash("Too many failed attempts. Please request a new verification code.", "error")
            return redirect(url_for("register") if action == "register" else url_for("forgot_password"))
            
        expires_at = datetime.strptime(otp_row["expires_at"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() > expires_at:
            with get_db() as conn:
                conn.execute("DELETE FROM otp_verifications WHERE email=?", (email,))
            session.pop("pending_reg", None)
            flash("Verification code has expired. Please try again.", "error")
            return redirect(url_for("register") if action == "register" else url_for("forgot_password"))
            
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        if code_hash != otp_row["otp_hash"]:
            attempts += 1
            with get_db() as conn:
                conn.execute("UPDATE otp_verifications SET attempts=? WHERE email=?", (attempts, email))
            print(f"[PASSWORD RESET] OTP verification failed for: {email} (Attempt {attempts}/3)", flush=True)
            flash(f"Incorrect code. Attempts remaining: {3 - attempts}", "error")
            return render_template("verify_otp.html", email=email)
            
        with get_db() as conn:
            conn.execute("DELETE FROM otp_verifications WHERE email=?", (email,))
        print(f"[PASSWORD RESET] OTP verified successfully for: {email}", flush=True)
            
        if action == "register":
            try:
                with get_db() as conn:
                    sql = "INSERT INTO users (name, email, password, is_admin) VALUES (?, ?, ?, 0)"
                    if os.environ.get("DATABASE_URL"):
                        sql += " RETURNING id"
                    cursor = conn.execute(
                        sql,
                        (pending["name"], email, pending["password_hash"])
                    )
                    user_id = cursor.lastrowid
                    user_name = pending["name"]
                
                session.pop("pending_reg", None)
                session["user_id"] = user_id
                session["user_name"] = user_name
                session["is_admin"] = False
                session.permanent = True
                
                flash("Registration successful! Welcome to Class Voice.", "success")
                return redirect(url_for("feed"))
            except (sqlite3.IntegrityError, psycopg2.IntegrityError):
                flash("Email already registered during this process. Please log in.", "error")
                session.pop("pending_reg", None)
                return redirect(url_for("login"))
        elif action == "forgot":
            session["pending_reg"]["verified"] = True
            session.modified = True
            return redirect(url_for("reset_password"))
            
    return render_template("verify_otp.html", email=email, dev_otp=session.get("dev_otp"))

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if "user_id" in session:
        return redirect(url_for("feed"))
        
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email:
            flash("Please enter your email address.", "error")
            return render_template("forgot_password.html")
            
        with get_db() as conn:
            user = conn.execute("SELECT id, is_banned FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            flash("Email address not found.", "error")
            return render_template("forgot_password.html")
            
        if user["is_banned"]:
            flash("Your account has been banned.", "error")
            return redirect(url_for("login"))
            
        print(f"[PASSWORD RESET] Forgot password request received for: {email}", flush=True)
            
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            otp_row = conn.execute("SELECT last_sent FROM otp_verifications WHERE email=?", (email,)).fetchone()
        if otp_row:
            last_sent = datetime.strptime(otp_row["last_sent"], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - last_sent).total_seconds() < 60:
                flash("Please wait 60 seconds before requesting another code.", "error")
                return render_template("forgot_password.html")
                
        otp = f"{random.randint(100000, 999999)}"
        print(f"[PASSWORD RESET] Generated OTP: {otp} for: {email}", flush=True)
        otp_hash = hashlib.sha256(otp.encode("utf-8")).hexdigest()
        expires_at = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        
        with get_db() as conn:
            conn.execute("""
                INSERT INTO otp_verifications (email, otp_hash, expires_at, attempts, last_sent)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(email) DO UPDATE SET
                    otp_hash=excluded.otp_hash,
                    expires_at=excluded.expires_at,
                    attempts=0,
                    last_sent=excluded.last_sent
            """, (email, otp_hash, expires_at, now_str))
            
        session["pending_reg"] = {
            "action": "forgot",
            "email": email,
            "verified": False
        }
        if FLASK_ENV == "development":
            session["dev_otp"] = otp
        
        if send_verification_email(email, otp):
            flash("Verification code sent to your email.", "success")
            return redirect(url_for("verify_otp"))
        else:
            flash("Failed to send verification email. Please contact support or check SMTP settings.", "error")
            return render_template("forgot_password.html")
        
    return render_template("forgot_password.html")

@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if "pending_reg" not in session or not session["pending_reg"].get("verified") or session["pending_reg"].get("action") != "forgot":
        flash("Please verify your email first.", "error")
        return redirect(url_for("forgot_password"))
        
    email = session["pending_reg"]["email"]
    
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        
        if not password or not confirm_password:
            flash("Please enter both password fields.", "error")
            return render_template("reset_password.html")
            
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("reset_password.html")
            
        pw_hash = generate_password_hash(password)
        with get_db() as conn:
            conn.execute("UPDATE users SET password=? WHERE email=?", (pw_hash, email))
        print(f"[PASSWORD RESET] Password successfully updated in DB for: {email}", flush=True)
            
        session.pop("pending_reg", None)
        flash("Password reset successful! Please log in with your new password.", "success")
        return redirect(url_for("login"))
        
    return render_template("reset_password.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Routes: Student Chat ─────────────────────────────────────────────────────

@app.route("/feed")
@login_required
def feed():
    user_id = session["user_id"]
    with get_db() as conn:
        user = conn.execute("SELECT is_muted, is_banned FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["is_banned"]:
            session.clear()
            return redirect(url_for("login"))
            
        # Load message history
        if os.environ.get("DATABASE_URL"):
            query = """
                SELECT p.id, p.user_id, p.message, p.filename, p.orig_name, p.created_at
                FROM posts p
                WHERE p.deleted = 0 AND CAST(p.created_at AS TIMESTAMPTZ) >= NOW() - INTERVAL '1 hour'
                ORDER BY p.created_at ASC
            """
        else:
            query = """
                SELECT p.id, p.user_id, p.message, p.filename, p.orig_name, p.created_at
                FROM posts p
                WHERE p.deleted = 0 AND datetime(p.created_at) >= datetime('now', '-1 hour')
                ORDER BY p.created_at ASC
            """
        posts = conn.execute(query).fetchall()
        
    return render_template("feed.html", posts=posts, is_muted=bool(user["is_muted"]))

@app.route("/upload", methods=["POST"])
@login_required
def upload_file():
    user_id = session["user_id"]
    with get_db() as conn:
        user = conn.execute("SELECT is_muted, is_banned FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user["is_banned"]:
        abort(403)
    if user["is_muted"]:
        return {"error": "You are muted and cannot upload files."}, 403
        
    file = request.files.get("file")
    if not file or not file.filename:
        return {"error": "No file selected."}, 400
        
    if not allowed_file(file.filename):
        return {"error": "File type not allowed. Supported: PDF, DOCX, PPTX, PNG, JPG."}, 400
        
    orig_name = secure_filename(file.filename)
    ext = orig_name.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    
    return {"filename": filename, "orig_name": orig_name}

@app.route("/uploads/<filename>")
@login_required
def uploaded_file(filename):
    with get_db() as conn:
        row = conn.execute("SELECT id FROM posts WHERE filename=? AND deleted=0", (filename,)).fetchone()
    if not row:
        abort(404)
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# ── Routes: Admin ─────────────────────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin_dashboard():
    with get_db() as conn:
        if os.environ.get("DATABASE_URL"):
            query = """
                SELECT p.id, p.message, p.filename, p.orig_name, p.created_at, p.user_id
                FROM posts p
                WHERE p.deleted = 0 AND CAST(p.created_at AS TIMESTAMPTZ) >= NOW() - INTERVAL '1 hour'
                ORDER BY p.created_at DESC
            """
        else:
            query = """
                SELECT p.id, p.message, p.filename, p.orig_name, p.created_at, p.user_id
                FROM posts p
                WHERE p.deleted = 0 AND datetime(p.created_at) >= datetime('now', '-1 hour')
                ORDER BY p.created_at DESC
            """
        posts = conn.execute(query).fetchall()
        
        users = conn.execute("""
            SELECT id, name, email, roll_no, is_muted, is_banned
            FROM users
            WHERE is_admin = 0
            ORDER BY name ASC
        """).fetchall()
        
        logs = conn.execute("""
            SELECT l.action, l.note, l.ts, u.name AS admin_name, u.email AS admin_email
            FROM admin_log l
            JOIN users u ON l.admin_id = u.id
            ORDER BY l.ts DESC
        """).fetchall()
        
    log_admin_action(session["user_id"], "VIEW_DASHBOARD")
    return render_template("admin.html", posts=posts, users=users, logs=logs)

@app.route("/admin/reveal-identity/<int:post_id>", methods=["POST"])
@admin_required
def admin_reveal_identity(post_id):
    with get_db() as conn:
        post = conn.execute("SELECT user_id, message FROM posts WHERE id=?", (post_id,)).fetchone()
        if not post:
            return {"error": "Message not found."}, 404
        user_id = post["user_id"]
        user = conn.execute("SELECT id, name, email, roll_no FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return {"error": "User not found."}, 404
            
    log_admin_action(
        session["user_id"],
        "VIEW_IDENTITY",
        target_id=user_id,
        note=f"Revealed identity of message #{post_id}: '{post['message'][:30]}...'"
    )
    
    return {
        "name": user["name"],
        "email": user["email"],
        "roll_no": user["roll_no"] or "—"
    }

@app.route("/admin/delete/<int:post_id>", methods=["POST"])
@admin_required
def admin_delete_post(post_id):
    with get_db() as conn:
        post = conn.execute("SELECT id, user_id FROM posts WHERE id=? AND deleted=0", (post_id,)).fetchone()
        if not post:
            return {"error": "Message not found or already deleted."}, 404
        conn.execute("UPDATE posts SET deleted=1 WHERE id=?", (post_id,))
        
    log_admin_action(
        session["user_id"],
        "DELETE_MESSAGE",
        target_id=post_id,
        note=f"Deleted message #{post_id} from user ID {post['user_id']}"
    )
    
    socketio.emit('message_deleted', {'id': post_id})
    return {"success": True}

@app.route("/admin/mute/<int:user_id>", methods=["POST"])
@admin_required
def admin_mute_user(user_id):
    with get_db() as conn:
        user = conn.execute("SELECT id, name FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return {"error": "User not found."}, 404
        conn.execute("UPDATE users SET is_muted=1 WHERE id=?", (user_id,))
        
    log_admin_action(session["user_id"], "MUTE_USER", target_id=user_id, note=f"Muted student {user['name']}")
    socketio.emit('user_muted_status', {'user_id': user_id, 'is_muted': True})
    return {"success": True}

@app.route("/admin/unmute/<int:user_id>", methods=["POST"])
@admin_required
def admin_unmute_user(user_id):
    with get_db() as conn:
        user = conn.execute("SELECT id, name FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return {"error": "User not found."}, 404
        conn.execute("UPDATE users SET is_muted=0 WHERE id=?", (user_id,))
        
    log_admin_action(session["user_id"], "UNMUTE_USER", target_id=user_id, note=f"Unmuted student {user['name']}")
    socketio.emit('user_muted_status', {'user_id': user_id, 'is_muted': False})
    return {"success": True}

@app.route("/admin/ban/<int:user_id>", methods=["POST"])
@admin_required
def admin_ban_user(user_id):
    with get_db() as conn:
        user = conn.execute("SELECT id, name FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return {"error": "User not found."}, 404
        conn.execute("UPDATE users SET is_banned=1 WHERE id=?", (user_id,))
        
    log_admin_action(session["user_id"], "BAN_USER", target_id=user_id, note=f"Banned student {user['name']}")
    socketio.emit('user_banned_status', {'user_id': user_id})
    
    if user_id in connected_users:
        sids = list(connected_users[user_id])
        for sid in sids:
            try:
                socketio.disconnect(sid)
            except Exception:
                pass
        del connected_users[user_id]
        emit_online_count()
        
    return {"success": True}

@app.route("/admin/unban/<int:user_id>", methods=["POST"])
@admin_required
def admin_unban_user(user_id):
    with get_db() as conn:
        user = conn.execute("SELECT id, name FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return {"error": "User not found."}, 404
        conn.execute("UPDATE users SET is_banned=0 WHERE id=?", (user_id,))
        
    log_admin_action(session["user_id"], "UNBAN_USER", target_id=user_id, note=f"Unbanned student {user['name']}")
    return {"success": True}

# ── Seed admin (dev only) ─────────────────────────────────────────────────────

@app.route("/dev/seed-admin")
def seed_admin():
    if os.environ.get("FLASK_ENV") != "development":
        abort(403)
    import secrets
    temp_password = secrets.token_urlsafe(16)
    pw = generate_password_hash(temp_password)
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO users (name, email, password, is_admin) VALUES (?,?,?,1)",
                ("Admin", "bl.sc.u4aie25240@bl.students.amrita.edu", pw)
            )
        return f"Admin seeded: bl.sc.u4aie25240@bl.students.amrita.edu / {temp_password}"
    except (sqlite3.IntegrityError, psycopg2.IntegrityError):
        return "Admin already exists."

# ── Error pages ───────────────────────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, msg="You don't have permission to view this page."), 403

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, msg="Page or file not found."), 404

@app.errorhandler(413)
def too_large(e):
    flash("File exceeds the 10 MB limit.", "error")
    return redirect(url_for("feed"))

# ── Socket.IO Handlers ─────────────────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    ensure_cleanup_task_started()
    if 'user_id' not in session:
        return False
    user_id = session['user_id']
    
    with get_db() as conn:
        user = conn.execute("SELECT is_banned FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user['is_banned']:
        return False
        
    sid = request.sid
    if user_id not in connected_users:
        connected_users[user_id] = set()
    connected_users[user_id].add(sid)
    
    emit_online_count()

@socketio.on('disconnect')
def handle_disconnect():
    user_id = session.get('user_id')
    sid = request.sid
    if user_id in connected_users:
        connected_users[user_id].discard(sid)
        if not connected_users[user_id]:
            del connected_users[user_id]
    emit_online_count()

def emit_online_count():
    count = len(connected_users)
    socketio.emit('online_count', {'count': count})

@socketio.on('send_message')
def handle_send_message(data):
    if 'user_id' not in session:
        return
    user_id = session['user_id']
    
    with get_db() as conn:
        user = conn.execute("SELECT is_muted, is_banned FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user['is_banned']:
        disconnect()
        return
    if user['is_muted']:
        emit('error', {'message': 'You are currently muted and cannot send messages.'})
        return
        
    # Rate Limiting: 5 messages per 10 seconds per user
    now = datetime.now().timestamp()
    if user_id not in rate_limits:
        rate_limits[user_id] = []
    
    rate_limits[user_id] = [t for t in rate_limits[user_id] if now - t < 10]
    
    if len(rate_limits[user_id]) >= 5:
        emit('rate_limited', {'message': 'Rate limit exceeded. Please wait before sending more messages.'})
        return
        
    rate_limits[user_id].append(now)
    
    message = data.get('message', '').strip()
    filename = data.get('filename')
    orig_name = data.get('orig_name')
    
    if not message and not filename:
        return
        
    message_escaped = html.escape(message)
    
    created_at = datetime.now(timezone.utc).isoformat()
    sql = "INSERT INTO posts (user_id, message, filename, orig_name, created_at) VALUES (?,?,?,?,?)"
    if os.environ.get("DATABASE_URL"):
        sql += " RETURNING id"
    with get_db() as conn:
        cursor = conn.execute(
            sql,
            (user_id, message_escaped, filename, orig_name, created_at)
        )
        post_id = cursor.lastrowid
        
    socketio.emit('new_message', {
        'id': post_id,
        'user_id': user_id,
        'message': message_escaped,
        'filename': filename,
        'orig_name': orig_name,
        'created_at': created_at
    })

def cleanup_expired_messages():
    print("[CLEANUP] Background task started.", flush=True)
    while True:
        socketio.sleep(60)
        try:
            with get_db() as conn:
                if os.environ.get("DATABASE_URL"):
                    query = "SELECT id, filename FROM posts WHERE CAST(created_at AS TIMESTAMPTZ) < NOW() - INTERVAL '1 hour'"
                else:
                    query = "SELECT id, filename FROM posts WHERE datetime(created_at) < datetime('now', '-1 hour')"
                expired_posts = conn.execute(query).fetchall()
                
                if expired_posts:
                    print(f"[CLEANUP] Found {len(expired_posts)} expired messages.", flush=True)
                    for post in expired_posts:
                        post_id = post["id"]
                        filename = post["filename"]
                        
                        if filename:
                            file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                            if os.path.exists(file_path):
                                try:
                                    os.remove(file_path)
                                    print(f"[CLEANUP] Deleted file: {file_path}", flush=True)
                                except Exception as e:
                                    print(f"[CLEANUP] Error deleting file {file_path}: {e}", flush=True)
                                    
                        conn.execute("DELETE FROM posts WHERE id=?", (post_id,))
                        print(f"[CLEANUP] Deleted message ID {post_id} from database.", flush=True)
                        socketio.emit('message_deleted', {'id': post_id})
                    conn.commit()
        except Exception as e:
            print(f"[CLEANUP] Error during cleanup: {e}", flush=True)

cleanup_task_started = False
cleanup_task_lock = threading.Lock()

def ensure_cleanup_task_started():
    global cleanup_task_started
    if not cleanup_task_started:
        with cleanup_task_lock:
            if not cleanup_task_started:
                socketio.start_background_task(cleanup_expired_messages)
                cleanup_task_started = True

@app.before_request
def start_cleanup_task_on_request():
    ensure_cleanup_task_started()

# ── Main Entry ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)
