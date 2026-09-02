import asyncio
import os
import requests
import time
from unittest.mock import MagicMock, patch

# --- הגדרות השרת המקומי ---
SERVER_URL = "https://whatsapptoosx.onrender.com/webhook"
HEALTH_URL = "https://whatsapptoosx.onrender.com/health"

# פיילוד מדמה מ-Green API הכולל נתוני הודעה עם דיווח על תקלה
MOCK_GREENAPI_PAYLOAD = {
    "typeWebhook": "incomingMessageReceived",
    "instanceData": {
        "idInstance": 710722725742,
        "wid": "710722725742@c.us",
        "typeInstance": "whatsapp"
    },
    "timestamp": 1690000000,
    "idMessage": "TEST_12113",
    "senderData": {
        "chatId": "0547878258@c.us",
        "sender": "0547878258@c.us",
        "senderName": "ישראל ישראלי"
    },
    "messageData": {
        "typeMessage": "textMessage",
        "textMessageData": {
            "textMessage": "שלום יש לי תקלה"
        }
    }
}

def run_e2e_test():
    print("=" * 60)
    print("🚀 מתחיל בדיקת E2E מלאה מקצה לקצה (זיהוי הודעה -> יצירת קריאה ב-OXS)")
    print("=" * 60)

    # 1. בדיקת זמינות השרת
    try:
        health_resp = requests.get(HEALTH_URL, timeout=3)
        if health_resp.status_code != 200:
            print(f"❌ השרת אינו זמין בכתובת {HEALTH_URL} (Status Code: {health_resp.status_code})")
            print("💡 יש לוודא שהשרת רץ ברקע מפקודת: python main.py")
            return
        print("✅ השרת פועל וזמין לקבלת בקשות (Health check OK)")
    except Exception as e:
        print(f"❌ לא ניתן להתחבר לשרת ב-localhost: {e}")
        print("💡 אנא הרץ את 'python main.py' בחלון טרמינל נפרד ונסה שוב.")
        return

    # 2. שליחת הודעת הדמה ל-Webhook
    print("\n📩 [שלב 1] שולח הודעת דמה על תקלה ב-WhatsApp ל-Endpoint /webhook...")
    headers = {"Content-Type": "application/json"}
    
    response = requests.post(SERVER_URL, json=MOCK_GREENAPI_PAYLOAD, headers=headers)
    
    print(f"   Status Code: {response.status_code}")
    print(f"   Response Body: {response.text}")

    if response.status_code == 200:
        res_json = response.json()
        if res_json.get("status") == "received" and res_json.get("accepted", 0) > 0:
            print("✅ [שלב 1 הצליח] ההודעה נקלטה ב-Webhook, עברה סיווג כתקלה והוכנסה לתור הטיפול!")
        else:
            print(f"⚠️ ההודעה נקלטה אך לא התקבלה לעיבוד. תשובה: {res_json}")
            return
    else:
        print(f"❌ הבקשה ל-Webhook נכשלה עם סטטוס {response.status_code}")
        return

    # 3. מעקב אחר העיבוד של השרת
    print("\n⏳ [שלב 2] המערכת מעבדת את ההודעה מתוך התור (חיפוש דייר ב-OXS ויצירת קריאה)...")
    print("   משראה 3 שניות לעיבוד הרקע בשרת...")
    time.sleep(3)

    print("\n" + "=" * 60)
    print("🎉 בדיקת הרשת והסימולציה הסתיימה!")
    print("📌 עכשיו בדוק את הטרמינל שבו רץ השרת (python main.py). אתה אמור לראות לוגים מהסוג:")
    print("   1. event=message.accepted ...")
    print("   2. event=tenant.found / event=oxs.service_call_created ...")
    print("=" * 60)

if __name__ == "__main__":
    run_e2e_test()