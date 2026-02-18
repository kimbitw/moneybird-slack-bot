import os
import requests

MONEYBIRD_TOKEN = os.environ["MONEYBIRD_TOKEN"]
ADMINISTRATION_ID = os.environ["MONEYBIRD_ADMINISTRATION_ID"]
BASE_URL = f"https://moneybird.com/api/v2/{ADMINISTRATION_ID}"

HEADERS = {
    "Authorization": f"Bearer {MONEYBIRD_TOKEN}",
    "Content-Type": "application/json",
}


def _get(path, params=None):
    r = requests.get(f"{BASE_URL}/{path}", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def _patch(path, payload):
    r = requests.patch(f"{BASE_URL}/{path}", headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


# --- Document fetchers ---

def get_receipt(receipt_id):
    return _get(f"documents/receipts/{receipt_id}")


def get_typeless_document(doc_id):
    return _get(f"documents/typeless_documents/{doc_id}")


def get_purchase_invoice(invoice_id):
    return _get(f"documents/purchase_invoices/{invoice_id}")


def get_attachment_content(document_type, document_id, attachment_id):
    """Returns (bytes, content_type) of the attachment."""
    url = f"{BASE_URL}/documents/{document_type}/{document_id}/attachments/{attachment_id}/download"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    content_type = r.headers.get("Content-Type", "application/octet-stream")
    return r.content, content_type


# --- Document booking ---

def book_receipt(receipt_id):
    """Mark receipt as booked."""
    return _patch(f"documents/receipts/{receipt_id}", {"receipt": {"state": "booked"}})


def book_purchase_invoice(invoice_id):
    """Mark purchase invoice as booked."""
    return _patch(
        f"documents/purchase_invoices/{invoice_id}",
        {"purchase_invoice": {"state": "booked"}},
    )


# --- Payment matching candidates ---

def get_unreconciled_payments():
    """
    Return unreconciled financial mutations (bank transactions) that could be
    matched against the document's amount.
    """
    try:
        mutations = _get("financial_mutations", params={"filter": "state:unprocessed"})
        return mutations if isinstance(mutations, list) else []
    except Exception:
        return []


def find_payment_candidates(amount_cents, contact_name=None, tolerance_cents=100):
    """
    Find unreconciled bank transactions matching the amount (±tolerance).
    Returns a list of candidate dicts with keys: id, date, amount, description.
    """
    mutations = get_unreconciled_payments()
    candidates = []
    for m in mutations:
        try:
            m_amount = int(float(m.get("amount", "0").replace(",", ".")) * 100)
        except (ValueError, AttributeError):
            continue

        if abs(m_amount - amount_cents) <= tolerance_cents:
            candidates.append({
                "id": m.get("id"),
                "date": m.get("date"),
                "amount": m.get("amount"),
                "description": m.get("message") or m.get("description") or "",
                "contact": m.get("contact", {}).get("company_name") if m.get("contact") else "",
            })

    return candidates


def link_payment_to_receipt(receipt_id, financial_mutation_id):
    """Link a bank transaction to a receipt as payment."""
    return _patch(
        f"documents/receipts/{receipt_id}",
        {
            "receipt": {
                "financial_mutations_attributes": [
                    {"financial_mutation_id": financial_mutation_id}
                ]
            }
        },
    )


def link_payment_to_purchase_invoice(invoice_id, financial_mutation_id):
    """Link a bank transaction to a purchase invoice as payment."""
    return _patch(
        f"documents/purchase_invoices/{invoice_id}",
        {
            "purchase_invoice": {
                "payments_attributes": [
                    {"financial_mutation_id": financial_mutation_id}
                ]
            }
        },
    )
