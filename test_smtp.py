import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Load .env file manually
def load_env():
    env_path = ".env"
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

SMTP_SERVER = os.environ.get("SMTP_SERVER")
SMTP_PORT = os.environ.get("SMTP_PORT")
SMTP_EMAIL = os.environ.get("SMTP_EMAIL")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
RECIPIENT = "bl.sc.u4aie25240@bl.students.amrita.edu"

print("--- SMTP Diagnostic Tool ---")
print(f"SMTP_SERVER: {SMTP_SERVER}")
print(f"SMTP_PORT: {SMTP_PORT}")
print(f"SMTP_EMAIL: {SMTP_EMAIL}")
print(f"SMTP_PASSWORD: {'*****' if SMTP_PASSWORD else 'None'}")
print(f"Sending test email to: {RECIPIENT}")
print("----------------------------")

if not all([SMTP_SERVER, SMTP_PORT, SMTP_EMAIL, SMTP_PASSWORD]):
    print("Error: Missing SMTP configuration in .env file.")
    print("Please create a .env file with the following keys:")
    print("SMTP_SERVER=...")
    print("SMTP_PORT=...")
    print("SMTP_EMAIL=...")
    print("SMTP_PASSWORD=...")
    exit(1)

try:
    port = int(SMTP_PORT)
except ValueError:
    print(f"Error: Invalid SMTP_PORT '{SMTP_PORT}' (must be an integer).")
    exit(1)

# Format standard MIME message
msg = MIMEMultipart()
msg['From'] = SMTP_EMAIL
msg['To'] = RECIPIENT
msg['Subject'] = "Class Voice SMTP Verification Test"
body = "This is a real SMTP delivery test for Class Voice email OTP verification."
msg.attach(MIMEText(body, 'plain'))
message_text = msg.as_string()

try:
    print(f"Connecting to SMTP server {SMTP_SERVER}:{port}...")
    context = ssl.create_default_context()
    
    if port == 465:
        # SSL
        with smtplib.SMTP_SSL(SMTP_SERVER, port, context=context, timeout=15) as server:
            print("Connection successful (SSL). Logging in...")
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            print("Login successful. Sending email...")
            server.sendmail(SMTP_EMAIL, RECIPIENT, message_text)
    else:
        # STARTTLS
        with smtplib.SMTP(SMTP_SERVER, port, timeout=15) as server:
            server.ehlo()
            print("Connection successful. Securing connection with STARTTLS...")
            server.starttls(context=context)
            server.ehlo()
            print("Connection secured. Logging in...")
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            print("Login successful. Sending email...")
            server.sendmail(SMTP_EMAIL, RECIPIENT, message_text)
            
    print("Success: Test email sent and accepted by the SMTP provider.")
    print("Please verify if it arrives in the Inbox or Junk folder.")
except Exception as e:
    print("\nSMTP Failure Diagnostic:")
    print(f"Error Type: {type(e).__name__}")
    print(f"Error Message: {e}")
    exit(1)
