# Class Voice

Anonymous class communication platform — students post anonymously, admins can identify authors.

---

## Local Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your college email domain
export COLLEGE_DOMAIN=bl.students.amrita.edu  # e.g. vitap.ac.in

# 3. Run in dev mode (creates DB + uploads folder automatically)
FLASK_ENV=development python app.py
```

Visit `http://localhost:5000`.

**Seed an admin account (dev only):**
```
GET http://localhost:5000/dev/seed-admin
```
This creates `admin@<COLLEGE_DOMAIN>` / `admin123`. Change the password immediately.

---

## Deployment on Render

1. Push this folder to a GitHub repo.
2. In Render → New Web Service → connect the repo.
3. Set env vars:
   - `COLLEGE_DOMAIN` → your actual domain (e.g. `vitap.ac.in`)
   - `SECRET_KEY` → any long random string
4. Render auto-runs `gunicorn app:app`.

**Persistent disk:** SQLite DB and uploads live on disk. Render's free tier does NOT persist disk — use the paid Starter plan or migrate to PostgreSQL + S3 for production.

---

## Admin Access

- Admins are flagged in the DB (`is_admin = 1`).
- Every admin action (dashboard view, post deletion) is written to the `admin_log` table.
- To promote a student to admin, run directly on the DB:
  ```sql
  UPDATE users SET is_admin = 1 WHERE email = 'lecturer@college.edu';
  ```

---

## Security Notes

- Passwords are hashed with Werkzeug's PBKDF2.
- File uploads are served only to authenticated users.
- Uploaded filenames are replaced with UUIDs to prevent path guessing.
- `COLLEGE_DOMAIN` enforces institutional email only.
- The `/dev/seed-admin` route is blocked in production (`FLASK_ENV != development`).

---

## What's NOT included (by design)

Comments, likes, reactions, notifications, search, categories, user profiles, chat.
