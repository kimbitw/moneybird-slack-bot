"""
Run this once to register the Moneybird webhook.
Usage: python setup_webhook.py
"""
import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["MONEYBIRD_TOKEN"]
ADMIN_ID = os.environ["MONEYBIRD_ADMINISTRATION_ID"]
RENDER_URL = "https://moneybird-slack-bot.onrender.com/webhook"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

payload = {
    "webhook": {
        "url": RENDER_URL,
        "events": [
            "receipt_created",
            "receipt_updated",
            "purchase_invoice_created",
            "purchase_invoice_updated",
        ],
    }
}
r = requests.post(
    f"https://moneybird.com/api/v2/{ADMIN_ID}/webhooks",
    headers=headers,
    json=payload,
)
print(f"CREATE: {r.status_code}")
print(json.dumps(r.json(), indent=2))

