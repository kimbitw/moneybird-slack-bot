import os
import io
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]

client = WebClient(token=SLACK_BOT_TOKEN)

DOC_TYPE_LABEL = {
    "receipt": "Receipt",
    "purchase_invoice": "Purchase Invoice",
}


def upload_attachment(file_bytes: bytes, filename: str, channel_id: str) -> str | None:
    """Upload a file to Slack and return the permalink (or None on failure)."""
    try:
        resp = client.files_upload_v2(
            channel=channel_id,
            file=io.BytesIO(file_bytes),
            filename=filename,
            title=filename,
        )
        file_info = resp.get("file", {})
        return file_info.get("permalink")
    except SlackApiError:
        return None


def post_document_notification(
    doc_info: dict,
    journal: dict,
    payment_candidates: list,
    attachment_permalink: str | None = None,
) -> dict:
    """
    Post a Block Kit message with document details, journal entry suggestion,
    payment match candidates, and OK/NG buttons.

    Returns the Slack API response (contains ts and channel for later updates).
    """
    doc_type = doc_info.get("type", "receipt")
    label = DOC_TYPE_LABEL.get(doc_type, "Document")
    doc_id = doc_info.get("id", "")
    contact = doc_info.get("contact", "Unknown")
    date = doc_info.get("date", "Unknown")
    amount = doc_info.get("amount", "0")
    currency = doc_info.get("currency", "EUR")
    description = doc_info.get("description") or "—"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"New {label} received"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Vendor:*\n{contact}"},
                {"type": "mrkdwn", "text": f"*Date:*\n{date}"},
                {"type": "mrkdwn", "text": f"*Amount:*\n{currency} {amount}"},
                {"type": "mrkdwn", "text": f"*Description:*\n{description}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Journal Entry Suggestion*\n"
                    f"• Debit: `{journal.get('debit', '—')}`\n"
                    f"• Credit: `{journal.get('credit', '—')}`\n"
                    f"_{journal.get('explanation', '')}_"
                ),
            },
        },
    ]

    # Attachment link
    if attachment_permalink:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Attachment:* <{attachment_permalink}|View file>",
            },
        })

    # Payment matching candidates
    if payment_candidates:
        candidate_lines = []
        for i, c in enumerate(payment_candidates[:3]):
            verdict = c.get("verdict", "")
            verdict_icon = "🟢" if verdict.upper().startswith("YES") else "🟡"
            candidate_lines.append(
                f"{verdict_icon} `{c['date']}` — {c['amount']} — {c['description'] or 'No description'}"
            )
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Payment Match Candidates* (bank transactions with similar amount)\n"
                    + "\n".join(candidate_lines)
                ),
            },
        })
        # Add match buttons for top candidate
        top = payment_candidates[0]
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "💳 Link this payment"},
                    "style": "primary",
                    "action_id": "link_payment",
                    "value": f"{doc_type}:{doc_id}:{top['id']}",
                }
            ],
        })
    else:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_No matching bank transactions found._"},
        })

    # OK / NG buttons
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✅ OK — Book it"},
                "style": "primary",
                "action_id": "book_document",
                "value": f"{doc_type}:{doc_id}",
                "confirm": {
                    "title": {"type": "plain_text", "text": "Confirm booking"},
                    "text": {"type": "mrkdwn", "text": f"Book this {label} in Moneybird?"},
                    "confirm": {"type": "plain_text", "text": "Yes, book it"},
                    "deny": {"type": "plain_text", "text": "Cancel"},
                },
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "❌ NG — Skip"},
                "action_id": "skip_document",
                "value": f"{doc_type}:{doc_id}",
            },
        ],
    })

    resp = client.chat_postMessage(
        channel=SLACK_CHANNEL_ID,
        text=f"New {label} from {contact} — {currency} {amount}",
        blocks=blocks,
    )
    return resp


def update_message_booked(channel: str, ts: str, label: str, contact: str):
    """Replace the message with a '✅ Booked' confirmation."""
    client.chat_update(
        channel=channel,
        ts=ts,
        text=f"✅ {label} from *{contact}* has been booked in Moneybird.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✅ *{label}* from *{contact}* has been booked in Moneybird.",
                },
            }
        ],
    )


def update_message_skipped(channel: str, ts: str, label: str, contact: str):
    """Replace the message with a 'Skipped' notice."""
    client.chat_update(
        channel=channel,
        ts=ts,
        text=f"⏭ {label} from {contact} was skipped.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⏭ *{label}* from *{contact}* was skipped.",
                },
            }
        ],
    )


def update_message_payment_linked(channel: str, ts: str, label: str, contact: str, mutation_date: str):
    """Append a note that a payment was linked."""
    client.chat_update(
        channel=channel,
        ts=ts,
        text=f"💳 Payment linked for {label} from {contact}.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"💳 Payment linked for *{label}* from *{contact}*.\n"
                        f"Bank transaction date: {mutation_date}"
                    ),
                },
            }
        ],
    )
