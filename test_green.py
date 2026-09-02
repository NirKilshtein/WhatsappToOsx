import os
import requests
from dotenv import load_dotenv

load_dotenv()

val1 = os.getenv("GREENAPI_INSTANCE_ID", "")
val2 = os.getenv("GREENAPI_API_TOKEN", "") or os.getenv("GREENAPI_API_KEY", "")

INSTANCE_ID = val1 if val1.isdigit() else val2
API_TOKEN = val2 if val1.isdigit() else val1

# כתובת ה-Webhook החדשה ב-Render
RENDER_WEBHOOK_URL = "https://whatsapptoosx.onrender.com/webhook"

def set_render_webhook():
    url = f"https://api.green-api.com/waInstance{INSTANCE_ID}/setSettings/{API_TOKEN}"
    payload = {
        "webhookUrl": RENDER_WEBHOOK_URL,
        "incomingWebhook": "yes"
    }
    
    print(f"🔄 מעדכן את ה-Webhook ב-Green API לכתובת: {RENDER_WEBHOOK_URL}...")
    try:
        res = requests.post(url, json=payload, timeout=10)
        print(f"   Status Code: {res.status_code}")
        print(f"   Response: {res.text}")
        if res.status_code == 200:
            print("✅ ה-Webhook עודכן בהצלחה ל-Render!")
    except Exception as e:
        print(f"❌ שגיאה בהגדרת ה-Webhook: {e}")

if __name__ == "__main__":
    set_render_webhook()