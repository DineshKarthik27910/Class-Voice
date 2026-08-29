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
        regex = r"^bl\.s\.u4aie25\d{3}@bl\.students\.amrita\.edu$"
        
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

if __name__ == '__main__':
    unittest.main()
