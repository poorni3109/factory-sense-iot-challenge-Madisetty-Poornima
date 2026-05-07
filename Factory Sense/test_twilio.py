"""
test_twilio.py — Direct Twilio test. Run this to confirm if Twilio works.
No FastAPI, no simulator — just a raw API call.

Usage:
    python test_twilio.py
"""

import os
from dotenv import load_dotenv
load_dotenv()

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
FROM_NUM    = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
TO_NUM      = os.getenv("ALERT_WHATSAPP_TO")

print("\n=== Twilio Configuration Check ===")
print(f"ACCOUNT_SID : {ACCOUNT_SID[:10]}..." if ACCOUNT_SID else "ACCOUNT_SID : ❌ MISSING")
print(f"AUTH_TOKEN  : {AUTH_TOKEN[:6]}..."  if AUTH_TOKEN  else "AUTH_TOKEN  : ❌ MISSING")
print(f"FROM        : {FROM_NUM}")
print(f"TO          : {TO_NUM}" if TO_NUM else "TO          : ❌ MISSING")

if not all([ACCOUNT_SID, AUTH_TOKEN, TO_NUM]):
    print("\n❌ Missing env vars. Check your .env file.")
    exit(1)

print("\n=== Sending Test WhatsApp Message ===")
try:
    from twilio.rest import Client
    client = Client(ACCOUNT_SID, AUTH_TOKEN)
    msg = client.messages.create(
        body="🧪 FactorySense TEST — If you see this, Twilio is working correctly!",
        from_=FROM_NUM,
        to=TO_NUM,
    )
    print(f"✅ SUCCESS! Message SID: {msg.sid}")
    print(f"   Status: {msg.status}")
    print("\n👉 Check your WhatsApp now. Message should arrive in 5-10 seconds.")

except Exception as e:
    print(f"\n❌ FAILED: {e}")
    print("\n--- What this error means ---")
    err = str(e)
    if "21608" in err:
        print("👉 Your phone has NOT joined the Twilio sandbox.")
        print("   Fix: Open WhatsApp → message +14155238886 → send 'join <keyword>'")
        print("   Find keyword at: https://console.twilio.com → Messaging → Try it Out → Send a WhatsApp message")
    elif "20003" in err or "authenticate" in err.lower():
        print("👉 Wrong ACCOUNT_SID or AUTH_TOKEN.")
        print("   Fix: Copy them again from https://console.twilio.com (top of dashboard)")
    elif "21211" in err or "invalid" in err.lower():
        print("👉 ALERT_WHATSAPP_TO number format is wrong.")
        print("   Fix: Make sure it is exactly: whatsapp:+919493400704")
    else:
        print(f"   Raw error: {e}")
