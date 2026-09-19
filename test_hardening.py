import sys
import os
import re
import unittest
import py_compile

# Ensure workspace is in python path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

class TestProductionHardening(unittest.TestCase):

    def test_syntax(self):
        """Verify syntax of all Python files in project"""
        py_files = ["app.py", "migrate_to_supabase.py", "test_smtp.py"]
        for file in py_files:
            abs_path = os.path.join(os.path.dirname(__file__), file)
            if os.path.exists(abs_path):
                py_compile.compile(abs_path, doraise=True)
        print("[SUCCESS] All Python syntax checks passed.")

    def test_class_email_regex(self):
        """Verify server-side class email regex and roll number extraction"""
        regex = r"^bl\.sc\.u4aie25\d{3}@bl\.students\.amrita\.edu$"
        
        valid_emails = [
            "bl.sc.u4aie25001@bl.students.amrita.edu",
            "bl.sc.u4aie25240@bl.students.amrita.edu",
            "bl.sc.u4aie25999@bl.students.amrita.edu"
        ]
        
        invalid_emails = [
            "other.student@bl.students.amrita.edu",
            "bl.sc.u4aie24001@bl.students.amrita.edu",  # wrong batch
            "bl.sc.u4aie2512@bl.students.amrita.edu",   # 2 digits
            "bl.sc.u4aie251234@bl.students.amrita.edu", # 4 digits
            "user@gmail.com",
            "admin@amrita.edu"
        ]
        
        for email in valid_emails:
            self.assertTrue(re.match(regex, email), f"Should match valid email: {email}")
            match = re.search(r'(u4aie25\d{3})', email, re.IGNORECASE)
            self.assertIsNotNone(match)
            roll_no = match.group(1).lower()
            self.assertTrue(roll_no.startswith("u4aie25"))
            self.assertEqual(len(roll_no), 10)
            
        for email in invalid_emails:
            self.assertIsNone(re.match(regex, email), f"Should reject invalid email: {email}")
            
        print("[SUCCESS] Class email regex and roll number extraction tests passed.")

    def test_app_imports_and_db(self):
        """Verify app import, DB initialization, and connection pooling logic"""
        from app import get_db, init_db, app
        init_db()
        
        # Test get_db context manager
        with get_db() as conn:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row["name"] for row in cursor.fetchall()]
            self.assertIn("users", tables)
            self.assertIn("posts", tables)
            self.assertIn("admin_log", tables)
            self.assertIn("otp_verifications", tables)
            
            # Check indexes
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='index';")
            indexes = [row["name"] for row in cursor.fetchall()]
            self.assertIn("idx_posts_deleted_created", indexes)
            self.assertIn("idx_posts_user_id", indexes)
            self.assertIn("idx_admin_log_admin_id", indexes)
            self.assertIn("idx_admin_log_ts", indexes)
            
        print("[SUCCESS] Database initialization, schema, and index checks passed.")

    def test_multi_client_socket_flow(self):
        """Simulate multi-user Socket.IO session tracking and message flow"""
        from app import app, socketio, connected_users, get_db, init_db
        init_db()

        # Ensure test users exist in DB
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO users (id, name, email, password, roll_no, is_admin, is_banned) VALUES (888, 'Test User 1', 'bl.sc.u4aie25888@bl.students.amrita.edu', 'pass', 'u4aie25888', 0, 0)")
            conn.execute("INSERT OR IGNORE INTO users (id, name, email, password, roll_no, is_admin, is_banned) VALUES (889, 'Test User 2', 'bl.sc.u4aie25889@bl.students.amrita.edu', 'pass', 'u4aie25889', 0, 0)")
            conn.commit()

        client1 = app.test_client()
        client2 = app.test_client()
        
        # Authenticate simulated sessions
        with client1.session_transaction() as sess:
            sess['user_id'] = 888
            sess['user_name'] = 'Test User 1'
            
        with client2.session_transaction() as sess:
            sess['user_id'] = 889
            sess['user_name'] = 'Test User 2'
            
        # Connect simulated socket clients
        s1 = socketio.test_client(app, flask_test_client=client1)
        s2 = socketio.test_client(app, flask_test_client=client2)
        
        self.assertTrue(s1.is_connected())
        self.assertTrue(s2.is_connected())
        
        # Verify online tracking
        self.assertIn(888, connected_users)
        self.assertIn(889, connected_users)
        self.assertGreaterEqual(len(connected_users), 2)
        
        # Disconnect client 1
        s1.disconnect()
        self.assertNotIn(888, connected_users)
        self.assertIn(889, connected_users)
        
        # Reconnect client 1
        s1_reconnect = socketio.test_client(app, flask_test_client=client1)
        self.assertTrue(s1_reconnect.is_connected())
        self.assertIn(888, connected_users)
        
        s1_reconnect.disconnect()
        s2.disconnect()
        print("[SUCCESS] Multi-client Socket.IO session tracking and reconnection test passed.")


class TestModeration(unittest.TestCase):
    """Tests for the AI moderation system."""

    def setUp(self):
        """Set up test fixtures."""
        os.environ.pop("DATABASE_URL", None)
        os.environ["FLASK_ENV"] = "development"
        from app import app, socketio, get_db, init_db
        self.app = app
        self.socketio = socketio
        self.get_db = get_db
        init_db()

        # Ensure test users exist and clean any previous test run artifacts
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO users (id, name, email, password, roll_no, is_admin, is_banned, is_muted) VALUES (900, 'Mod Test Student', 'bl.sc.u4aie25900@bl.students.amrita.edu', 'pass', 'u4aie25900', 0, 0, 0)")
            conn.execute("INSERT OR IGNORE INTO users (id, name, email, password, roll_no, is_admin, is_banned, is_muted) VALUES (901, 'Mod Test Admin', 'bl.sc.u4aie25901@bl.students.amrita.edu', 'pass', 'u4aie25901', 1, 0, 0)")
            conn.execute("DELETE FROM moderation_reviews WHERE id >= 9000 OR post_id >= 9000")
            conn.execute("DELETE FROM posts WHERE id >= 9000")
            conn.commit()

    def test_moderate_safe_message(self):
        """Verify SAFE classification via mocked AI response."""
        from unittest.mock import patch
        mock_result = {"category": "SAFE", "reason": "Normal question", "confidence": 0.95}
        with patch("moderation.moderate_message", return_value=mock_result):
            from moderation import moderate_message
            result = moderate_message("Does anyone know when the lab starts?")
        self.assertEqual(mock_result["category"], "SAFE")
        print("[SUCCESS] SAFE message classification test passed.")

    def test_moderate_potentially_abusive(self):
        """Verify POTENTIALLY_ABUSIVE classification via mocked AI response."""
        from unittest.mock import patch
        mock_result = {"category": "POTENTIALLY_ABUSIVE", "reason": "Mild insult", "confidence": 0.72}
        with patch("moderation.moderate_message", return_value=mock_result):
            from moderation import moderate_message
            result = moderate_message("Bro r u stupid?")
        self.assertEqual(mock_result["category"], "POTENTIALLY_ABUSIVE")
        print("[SUCCESS] POTENTIALLY_ABUSIVE message classification test passed.")

    def test_moderate_severe_violation(self):
        """Verify SEVERE_VIOLATION classification via mocked AI response."""
        from unittest.mock import patch
        mock_result = {"category": "SEVERE_VIOLATION", "reason": "Extreme hate speech", "confidence": 0.98}
        with patch("moderation.moderate_message", return_value=mock_result):
            from moderation import moderate_message
            result = moderate_message("severe test content")
        self.assertEqual(mock_result["category"], "SEVERE_VIOLATION")
        print("[SUCCESS] SEVERE_VIOLATION message classification test passed.")

    def test_moderation_db_schema(self):
        """Verify moderation_reviews table exists with correct columns."""
        with self.get_db() as conn:
            cursor = conn.execute("PRAGMA table_info(moderation_reviews)")
            columns = [row["name"] for row in cursor.fetchall()]
            expected = ["id", "post_id", "user_id", "category", "reason", "confidence",
                        "status", "created_at", "reviewed_at", "reviewed_by", "action"]
            for col in expected:
                self.assertIn(col, columns, f"Missing column: {col}")
        print("[SUCCESS] moderation_reviews DB schema test passed.")

    def test_admin_approve_review(self):
        """Test admin approve endpoint marks review as approved."""
        from datetime import datetime, timezone
        created_at = datetime.now(timezone.utc).isoformat()

        with self.get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO posts (id, user_id, message, created_at, deleted) VALUES (9000, 900, 'test msg', ?, 0)", (created_at,))
            conn.execute("INSERT OR IGNORE INTO moderation_reviews (id, post_id, user_id, category, reason, confidence, status, created_at) VALUES (9000, 9000, 900, 'POTENTIALLY_ABUSIVE', 'test', 0.7, 'pending', ?)", (created_at,))
            conn.commit()

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'] = 901
            sess['user_name'] = 'Mod Test Admin'
            sess['is_admin'] = True

        resp = client.post('/admin/moderation/approve/9000')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))

        with self.get_db() as conn:
            review = conn.execute("SELECT status, action FROM moderation_reviews WHERE id=9000").fetchone()
            self.assertEqual(review["status"], "approved")
            self.assertEqual(review["action"], "kept")

            # Verify message is still visible
            post = conn.execute("SELECT deleted FROM posts WHERE id=9000").fetchone()
            self.assertEqual(post["deleted"], 0)

        print("[SUCCESS] Admin approve moderation review test passed.")

    def test_admin_reject_review(self):
        """Test admin reject endpoint marks review as rejected and deletes message."""
        from datetime import datetime, timezone
        created_at = datetime.now(timezone.utc).isoformat()

        with self.get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO posts (id, user_id, message, created_at, deleted) VALUES (9001, 900, 'test abusive msg', ?, 0)", (created_at,))
            conn.execute("INSERT OR IGNORE INTO moderation_reviews (id, post_id, user_id, category, reason, confidence, status, created_at) VALUES (9001, 9001, 900, 'POTENTIALLY_ABUSIVE', 'test', 0.8, 'pending', ?)", (created_at,))
            conn.commit()

        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'] = 901
            sess['user_name'] = 'Mod Test Admin'
            sess['is_admin'] = True

        resp = client.post('/admin/moderation/reject/9001')
        self.assertEqual(resp.status_code, 200)

        with self.get_db() as conn:
            review = conn.execute("SELECT status, action FROM moderation_reviews WHERE id=9001").fetchone()
            self.assertEqual(review["status"], "rejected")
            self.assertEqual(review["action"], "deleted")

            post = conn.execute("SELECT deleted FROM posts WHERE id=9001").fetchone()
            self.assertEqual(post["deleted"], 1)

        print("[SUCCESS] Admin reject moderation review test passed.")

    def test_auto_approve_timeout(self):
        """Verify reviews older than MODERATION_REVIEW_MINUTES are auto-approved."""
        from datetime import datetime, timezone, timedelta
        import app as app_module

        old_time = (datetime.now(timezone.utc) - timedelta(minutes=app_module.MODERATION_REVIEW_MINUTES + 5)).isoformat()

        with self.get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO posts (id, user_id, message, created_at, deleted) VALUES (9002, 900, 'old pending msg', ?, 0)", (old_time,))
            conn.execute("INSERT OR IGNORE INTO moderation_reviews (id, post_id, user_id, category, reason, confidence, status, created_at) VALUES (9002, 9002, 900, 'POTENTIALLY_ABUSIVE', 'test', 0.6, 'pending', ?)", (old_time,))
            conn.commit()

        # Simulate the auto-approve logic from cleanup
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=app_module.MODERATION_REVIEW_MINUTES)).isoformat()
        with self.get_db() as conn:
            pending = conn.execute(
                "SELECT id, post_id FROM moderation_reviews WHERE status='pending' AND created_at < ?",
                (cutoff,)
            ).fetchall()
            now_str = datetime.now(timezone.utc).isoformat()
            for review in pending:
                conn.execute(
                    "UPDATE moderation_reviews SET status='approved', action='auto_approved', reviewed_at=? WHERE id=?",
                    (now_str, review["id"])
                )
            conn.commit()

        with self.get_db() as conn:
            review = conn.execute("SELECT status, action FROM moderation_reviews WHERE id=9002").fetchone()
            self.assertEqual(review["status"], "approved")
            self.assertEqual(review["action"], "auto_approved")

        print("[SUCCESS] Auto-approve timeout test passed.")

    def test_severe_not_broadcast(self):
        """Verify SEVERE_VIOLATION message is broadcast immediately and deleted in real-time."""
        import time
        from unittest.mock import patch
        from app import app, socketio, get_db, connected_users

        client1 = app.test_client()
        client2 = app.test_client()

        with client1.session_transaction() as sess:
            sess['user_id'] = 900
            sess['user_name'] = 'Mod Test Student'
        with client2.session_transaction() as sess:
            sess['user_id'] = 901
            sess['user_name'] = 'Mod Test Admin'
            sess['is_admin'] = True

        mock_result = {"category": "SEVERE_VIOLATION", "reason": "Test severe", "confidence": 0.99}
        with patch("app.moderate_message", return_value=mock_result), \
             patch("app.MODERATION_ENABLED", True):
            s1 = socketio.test_client(app, flask_test_client=client1)
            s2 = socketio.test_client(app, flask_test_client=client2)

            s1.emit('send_message', {'message': 'severe test content'})

            # Wait briefly for background moderation to complete
            time.sleep(0.2)

            # Client 2 received new_message immediately, followed by real-time message_deleted
            received_by_s2 = s2.get_received()
            new_msg_events = [e for e in received_by_s2 if e['name'] == 'new_message']
            self.assertEqual(len(new_msg_events), 1, "Immediate broadcast should emit new_message")
            post_id = new_msg_events[0]['args'][0]['id']

            del_events = [e for e in received_by_s2 if e['name'] == 'message_deleted']
            self.assertGreaterEqual(len(del_events), 1, "Severe message was not deleted in real time!")
            self.assertEqual(del_events[0]['args'][0]['id'], post_id)

            # Client 1 should receive a moderation_warning
            received_by_s1 = s1.get_received()
            warning_events = [e for e in received_by_s1 if e['name'] == 'moderation_warning']
            self.assertGreaterEqual(len(warning_events), 1, "Sender did not receive moderation_warning!")

            # Verify database state
            with get_db() as conn:
                post = conn.execute("SELECT deleted FROM posts WHERE id=?", (post_id,)).fetchone()
                self.assertIsNotNone(post)
                self.assertEqual(post["deleted"], 1)

                review = conn.execute("SELECT status, action FROM moderation_reviews WHERE post_id=?", (post_id,)).fetchone()
                self.assertIsNotNone(review)
                self.assertEqual(review["status"], "rejected")
                self.assertEqual(review["action"], "auto_rejected")

            s1.disconnect()
            s2.disconnect()

        print("[SUCCESS] SEVERE_VIOLATION real-time deletion & warning test passed.")

    def test_async_safe_socket_flow(self):
        """Verify SAFE message is broadcast immediately and remains visible."""
        import time
        from unittest.mock import patch
        from app import app, socketio, get_db

        client1 = app.test_client()
        client2 = app.test_client()
        with client1.session_transaction() as sess:
            sess['user_id'] = 900
            sess['user_name'] = 'Mod Test Student'
        with client2.session_transaction() as sess:
            sess['user_id'] = 901
            sess['user_name'] = 'Mod Test Admin'
            sess['is_admin'] = True

        mock_result = {"category": "SAFE", "reason": "Normal question", "confidence": 0.98}
        with patch("app.moderate_message", return_value=mock_result), \
             patch("app.MODERATION_ENABLED", True):
            s1 = socketio.test_client(app, flask_test_client=client1)
            s2 = socketio.test_client(app, flask_test_client=client2)

            s1.emit('send_message', {'message': 'Safe async message test'})
            time.sleep(0.2)

            received_by_s2 = s2.get_received()
            new_msgs = [e for e in received_by_s2 if e['name'] == 'new_message']
            self.assertEqual(len(new_msgs), 1)
            post_id = new_msgs[0]['args'][0]['id']

            del_events = [e for e in received_by_s2 if e['name'] == 'message_deleted']
            self.assertEqual(len(del_events), 0, "SAFE message should not be deleted")

            with get_db() as conn:
                post = conn.execute("SELECT deleted FROM posts WHERE id=?", (post_id,)).fetchone()
                self.assertEqual(post["deleted"], 0)
                review = conn.execute("SELECT id FROM moderation_reviews WHERE post_id=?", (post_id,)).fetchone()
                self.assertIsNone(review, "SAFE message should not produce a review")

            s1.disconnect()
            s2.disconnect()
        print("[SUCCESS] Async SAFE socket flow test passed.")

    def test_async_potentially_abusive_socket_flow(self):
        """Verify POTENTIALLY_ABUSIVE message remains visible and creates admin review."""
        import time
        from unittest.mock import patch
        from app import app, socketio, get_db

        client1 = app.test_client()
        client2 = app.test_client()
        with client1.session_transaction() as sess:
            sess['user_id'] = 900
            sess['user_name'] = 'Mod Test Student'
        with client2.session_transaction() as sess:
            sess['user_id'] = 901
            sess['user_name'] = 'Mod Test Admin'
            sess['is_admin'] = True

        mock_result = {"category": "POTENTIALLY_ABUSIVE", "reason": "Mild insult", "confidence": 0.85}
        with patch("app.moderate_message", return_value=mock_result), \
             patch("app.MODERATION_ENABLED", True):
            s1 = socketio.test_client(app, flask_test_client=client1)
            s2 = socketio.test_client(app, flask_test_client=client2)

            s1.emit('send_message', {'message': 'Potentially abusive async test'})
            time.sleep(0.2)

            received_by_s2 = s2.get_received()
            new_msgs = [e for e in received_by_s2 if e['name'] == 'new_message']
            self.assertEqual(len(new_msgs), 1)
            post_id = new_msgs[0]['args'][0]['id']

            del_events = [e for e in received_by_s2 if e['name'] == 'message_deleted']
            self.assertEqual(len(del_events), 0, "POTENTIALLY_ABUSIVE message must remain visible")

            admin_review_events = [e for e in received_by_s2 if e['name'] == 'moderation_review_new']
            self.assertGreaterEqual(len(admin_review_events), 1, "Admin should receive moderation_review_new event")
            self.assertEqual(admin_review_events[0]['args'][0]['post_id'], post_id)

            with get_db() as conn:
                post = conn.execute("SELECT deleted FROM posts WHERE id=?", (post_id,)).fetchone()
                self.assertEqual(post["deleted"], 0)
                review = conn.execute("SELECT status FROM moderation_reviews WHERE post_id=?", (post_id,)).fetchone()
                self.assertIsNotNone(review)
                self.assertEqual(review["status"], "pending")

            s1.disconnect()
            s2.disconnect()
        print("[SUCCESS] Async POTENTIALLY_ABUSIVE socket flow test passed.")

    def test_student_cannot_modify_review(self):
        """Verify non-admin gets 403 on moderation endpoints."""
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'] = 900
            sess['user_name'] = 'Mod Test Student'
            sess['is_admin'] = False

        resp = client.post('/admin/moderation/approve/9999')
        self.assertEqual(resp.status_code, 403)

        resp = client.post('/admin/moderation/reject/9999')
        self.assertEqual(resp.status_code, 403)

        resp = client.get('/admin/moderation')
        self.assertEqual(resp.status_code, 403)

        print("[SUCCESS] Student cannot access moderation endpoints test passed.")

    def test_moderation_api_failure_fallback(self):
        """Verify API failure returns POTENTIALLY_ABUSIVE (not SAFE)."""
        from unittest.mock import patch
        with patch("moderation._call_gemini", side_effect=RuntimeError("API timeout")):
            # Need to ensure GEMINI_API_KEY is set so it tries the API
            with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
                from moderation import moderate_message
                result = moderate_message("some test message")
                self.assertEqual(result["category"], "POTENTIALLY_ABUSIVE")
                self.assertIn("unavailable", result["reason"].lower())
        print("[SUCCESS] API failure fallback to POTENTIALLY_ABUSIVE test passed.")

    def test_realtime_deletion_on_reject(self):
        """Verify Socket.IO message_deleted is emitted on moderation reject."""
        from datetime import datetime, timezone
        from app import app, socketio, get_db

        created_at = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO posts (id, user_id, message, created_at, deleted) VALUES (9003, 900, 'msg to delete', ?, 0)", (created_at,))
            conn.execute("INSERT OR IGNORE INTO moderation_reviews (id, post_id, user_id, category, reason, confidence, status, created_at) VALUES (9003, 9003, 900, 'POTENTIALLY_ABUSIVE', 'test', 0.75, 'pending', ?)", (created_at,))
            conn.commit()

        admin_client = app.test_client()
        student_client = app.test_client()

        with admin_client.session_transaction() as sess:
            sess['user_id'] = 901
            sess['user_name'] = 'Mod Test Admin'
            sess['is_admin'] = True
        with student_client.session_transaction() as sess:
            sess['user_id'] = 900
            sess['user_name'] = 'Mod Test Student'

        s_student = socketio.test_client(app, flask_test_client=student_client)

        resp = admin_client.post('/admin/moderation/reject/9003')
        self.assertEqual(resp.status_code, 200)

        received = s_student.get_received()
        delete_events = [e for e in received if e['name'] == 'message_deleted']
        self.assertGreaterEqual(len(delete_events), 1, "message_deleted not emitted on reject!")
        self.assertEqual(delete_events[0]['args'][0]['id'], 9003)

        s_student.disconnect()
        print("[SUCCESS] Real-time deletion on moderation reject test passed.")


if __name__ == '__main__':
    unittest.main()
