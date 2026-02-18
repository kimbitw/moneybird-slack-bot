import os
import json
import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def suggest_journal_entry(doc_info: dict) -> dict:
    """
    Ask Claude to suggest a journal entry for the document.

    doc_info keys: type, contact, date, amount, currency, description, line_items
    Returns: {"debit": str, "credit": str, "explanation": str}
    """
    doc_type = "Receipt" if doc_info.get("type") == "receipt" else "Purchase Invoice"
    line_items_text = ""
    for item in doc_info.get("line_items", []):
        line_items_text += f"  - {item.get('description', 'N/A')}: {item.get('total_amount', 'N/A')} {doc_info.get('currency', 'EUR')}\n"

    prompt = f"""You are an accountant. Based on the following {doc_type}, suggest a journal entry using English account names.

Document details:
- Type: {doc_type}
- Contact/Vendor: {doc_info.get('contact', 'Unknown')}
- Date: {doc_info.get('date', 'Unknown')}
- Total Amount: {doc_info.get('amount', '0')} {doc_info.get('currency', 'EUR')}
- Description: {doc_info.get('description', 'N/A')}
- Line items:
{line_items_text if line_items_text else '  (none)'}

Respond ONLY with a JSON object in this exact format:
{{
  "debit": "<account name, e.g. Office Supplies, Travel Expense, Utilities>",
  "credit": "<account name, e.g. Accounts Payable, Cash, Bank>",
  "explanation": "<one sentence explaining why>"
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {
            "debit": "Uncategorized Expense",
            "credit": "Accounts Payable",
            "explanation": raw,
        }


def suggest_payment_match(payment_candidate: dict, doc_info: dict) -> str:
    """
    Ask Claude whether a payment candidate looks like a good match.
    Returns a short verdict string.
    """
    prompt = f"""Does this bank transaction look like a payment for the document below?

Document:
- Contact: {doc_info.get('contact', 'Unknown')}
- Amount: {doc_info.get('amount', '0')} {doc_info.get('currency', 'EUR')}
- Date: {doc_info.get('date', 'Unknown')}

Bank transaction:
- Date: {payment_candidate.get('date')}
- Amount: {payment_candidate.get('amount')}
- Description: {payment_candidate.get('description')}
- Counter-party: {payment_candidate.get('contact') or 'Unknown'}

Reply in one short sentence (15 words max) starting with YES or NO."""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()
