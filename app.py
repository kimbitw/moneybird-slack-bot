import hmac
import hashlib
import json
import os
import time
import threading
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

import moneybird
import journal_ai
import slack_notifier

app = Flask(__name__)

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
DOC_TYPE_LABEL = {"receipt": "Receipt", "purchase_invoice": "Purchase Invoice"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def verify_slack_signature(req) -> bool:
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    if abs(time.time() - int(timestamp)) > 300:
        return False
    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    my_sig = (
        "v0="
        + hmac.new(
            SLACK_SIGNING_SECRET.encode(),
            sig_basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    slack_sig = req.headers.get("X-Slack-Signature", "")
    return hmac.compare_digest(my_sig, slack_sig)


def extract_doc_info(raw: dict, doc_type: str) -> dict:
    """Normalize receipt or purchase_invoice dict into a common structure."""
    contact_name = ""
    if raw.get("contact"):
        contact_name = raw["contact"].get("company_name") or raw["contact"].get("firstname", "")
    elif raw.get("contact_id"):
        contact_name = raw.get("contact_id", "")

    amount = raw.get("total_amount") or raw.get("total_amount_incl_tax") or "0"

    line_items = []
    for item in raw.get("details", []):
        line_items.append({
            "description": item.get("description", ""),
            "total_amount": item.get("total_amount_incl_tax") or item.get("total_amount", ""),
        })

    return {
        "id": raw.get("id"),
        "type": doc_type,
        "contact": contact_name,
        "date": raw.get("date") or raw.get("invoice_date", ""),
        "amount": amount,
        "currency": raw.get("currency") or "EUR",
        "description": raw.get("reference") or raw.get("invoice_sequence_identifier") or "",
        "line_items": line_items,
        "attachments": raw.get("attachments", []),
    }


def process_document(doc_type: str, doc_id: str):
    """Full async pipeline: fetch → AI → Slack."""
    try:
        if doc_type == "receipt":
            try:
                raw = moneybird.get_receipt(doc_id)
            except Exception:
                raw = moneybird.get_typeless_document(doc_id)
        else:
            raw = moneybird.get_purchase_invoice(doc_id)

        doc_info = extract_doc_info(raw, doc_type)

        # Upload first attachment to Slack
        attachment_permalink = None
        attachments = doc_info.get("attachments", [])
        if attachments:
            att = attachments[0]
            att_id = att.get("id")
            filename = att.get("filename") or f"attachment_{att_id}"
            file_bytes, _ = moneybird.get_attachment_content(
                f"{doc_type}s", doc_id, att_id
            )
            attachment_permalink = slack_notifier.upload_attachment(
                file_bytes, filename, slack_notifier.SLACK_CHANNEL_ID
            )

        # Journal entry suggestion
        journal = journal_ai.suggest_journal_entry(doc_info)

        # Payment match candidates
        try:
            amount_cents = int(float(doc_info["amount"].replace(",", ".")) * 100)
        except (ValueError, AttributeError):
            amount_cents = 0
        candidates = moneybird.find_payment_candidates(amount_cents, doc_info["contact"])

        # Add Claude verdict to each candidate
        for c in candidates[:3]:
            c["verdict"] = journal_ai.suggest_payment_match(c, doc_info)

        # Post to Slack
        slack_notifier.post_document_notification(
            doc_info=doc_info,
            journal=journal,
            payment_candidates=candidates,
            attachment_permalink=attachment_permalink,
        )

    except Exception as e:
        print(f"[ERROR] process_document({doc_type}, {doc_id}): {e}")


# ---------------------------------------------------------------------------
# Webhook endpoint (Moneybird → this app)
# ---------------------------------------------------------------------------

@app.route("/")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/webhook", methods=["POST", "GET"])
def webhook():
    # Moneybird sends a GET to verify the URL on registration
    if request.method == "GET":
        return jsonify({"status": "ok"}), 200

    payload = request.get_json(force=True) or {}

    print(f"[WEBHOOK] {json.dumps({k: payload.get(k) for k in ('entity_type','action','entity_id')})}")

    entity_type = payload.get("entity_type", "")
    action = payload.get("action", "")
    entity_id = str(payload.get("entity_id", ""))

    # Map Moneybird entity types to our doc types
    if entity_type == "Receipt" and action in ("created", "updated", "document_saved"):
        doc_type = "receipt"
    elif entity_type == "PurchaseInvoice" and action in ("created", "updated", "document_saved"):
        doc_type = "purchase_invoice"
    else:
        print(f"[WEBHOOK] ignored entity_type={entity_type} action={action}")
        return jsonify({"status": "ignored"}), 200

    # Process asynchronously so we return 200 immediately
    threading.Thread(target=process_document, args=(doc_type, entity_id), daemon=True).start()
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Slack Interactivity endpoint
# ---------------------------------------------------------------------------

@app.route("/slack/actions", methods=["POST"])
def slack_actions():
    if not verify_slack_signature(request):
        return jsonify({"error": "invalid signature"}), 403

    payload = json.loads(request.form.get("payload", "{}"))
    actions = payload.get("actions", [])
    if not actions:
        return jsonify({}), 200

    action = actions[0]
    action_id = action.get("action_id")
    value = action.get("value", "")

    channel = payload.get("container", {}).get("channel_id") or payload.get("channel", {}).get("id", "")
    ts = payload.get("container", {}).get("message_ts") or payload.get("message", {}).get("ts", "")

    # --- Book the document ---
    if action_id == "book_document":
        parts = value.split(":", 1)
        if len(parts) == 2:
            doc_type, doc_id = parts
            label = DOC_TYPE_LABEL.get(doc_type, "Document")
            try:
                if doc_type == "receipt":
                    raw = moneybird.get_receipt(doc_id)
                    doc_info = extract_doc_info(raw, doc_type)
                    moneybird.book_receipt(doc_id)
                else:
                    raw = moneybird.get_purchase_invoice(doc_id)
                    doc_info = extract_doc_info(raw, doc_type)
                    moneybird.book_purchase_invoice(doc_id)
                slack_notifier.update_message_booked(channel, ts, label, doc_info["contact"])
            except Exception as e:
                print(f"[ERROR] book_document: {e}")

    # --- Skip the document ---
    elif action_id == "skip_document":
        parts = value.split(":", 1)
        if len(parts) == 2:
            doc_type, doc_id = parts
            label = DOC_TYPE_LABEL.get(doc_type, "Document")
            try:
                if doc_type == "receipt":
                    raw = moneybird.get_receipt(doc_id)
                else:
                    raw = moneybird.get_purchase_invoice(doc_id)
                doc_info = extract_doc_info(raw, doc_type)
                slack_notifier.update_message_skipped(channel, ts, label, doc_info["contact"])
            except Exception as e:
                print(f"[ERROR] skip_document: {e}")

    # --- Link payment ---
    elif action_id == "link_payment":
        parts = value.split(":", 2)
        if len(parts) == 3:
            doc_type, doc_id, mutation_id = parts
            label = DOC_TYPE_LABEL.get(doc_type, "Document")
            try:
                if doc_type == "receipt":
                    raw = moneybird.get_receipt(doc_id)
                    doc_info = extract_doc_info(raw, doc_type)
                    moneybird.link_payment_to_receipt(doc_id, mutation_id)
                else:
                    raw = moneybird.get_purchase_invoice(doc_id)
                    doc_info = extract_doc_info(raw, doc_type)
                    moneybird.link_payment_to_purchase_invoice(doc_id, mutation_id)
                slack_notifier.update_message_payment_linked(
                    channel, ts, label, doc_info["contact"], mutation_date="—"
                )
            except Exception as e:
                print(f"[ERROR] link_payment: {e}")

    return jsonify({}), 200


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
