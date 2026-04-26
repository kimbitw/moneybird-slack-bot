"""
Moneybird Sales Invoice API wrapper.

Mirrors the relevant parts of GAS/moneybird-invoice/gas/MoneybirdAPI.js
for the Slack-driven invoice creation flow.

Reads MONEYBIRD_TOKEN and MONEYBIRD_ADMINISTRATION_ID from the env
(same as moneybird.py).
"""
import os
import requests

MONEYBIRD_TOKEN = os.environ["MONEYBIRD_TOKEN"]
ADMINISTRATION_ID = os.environ["MONEYBIRD_ADMINISTRATION_ID"]
BASE_URL = f"https://moneybird.com/api/v2/{ADMINISTRATION_ID}"

HEADERS = {
    "Authorization": f"Bearer {MONEYBIRD_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

_revenue_ledger_id_cache: str | None = None


def _request(method: str, path: str, payload: dict | None = None, params: dict | None = None):
    r = requests.request(
        method,
        f"{BASE_URL}{path}",
        headers=HEADERS,
        json=payload if payload is not None else None,
        params=params,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Moneybird {method} {path} failed ({r.status_code}): {r.text}")
    if r.status_code == 204 or not r.text:
        return None
    return r.json()


# ---- Contacts ----

def search_contacts(query: str | None = None, per_page: int = 25):
    params = {"per_page": per_page}
    if query:
        params["query"] = query
    return _request("GET", "/contacts.json", params=params)


def create_contact(attrs: dict):
    """
    attrs may include: company_name, firstname, lastname, address1, address2,
    zipcode, city, country, tax_number, send_invoices_to_email,
    send_estimates_to_email, customer_id, sepa_*.
    """
    return _request("POST", "/contacts.json", payload={"contact": attrs})


def get_contact(contact_id: str):
    return _request("GET", f"/contacts/{contact_id}.json")


# ---- Reference data ----

def list_tax_rates():
    return _request("GET", "/tax_rates.json")


def list_sales_tax_rates_filtered():
    """
    Return tax rates usable for sales invoices, filtered to the same set as the
    GAS tool: 9%, 21%, and No-VAT (0% with show_tax false).
    """
    rates = list_tax_rates() or []
    out = []
    for t in rates:
        if not t.get("active"):
            continue
        if t.get("tax_rate_type") != "sales_invoice":
            continue
        try:
            pct = float(t.get("percentage", "0"))
        except (TypeError, ValueError):
            continue
        if t.get("show_tax") is False and pct == 0:
            out.append({"id": t["id"], "name": t["name"], "percentage": pct, "label": "No VAT (0%)"})
        elif pct in (9.0, 21.0):
            out.append({"id": t["id"], "name": t["name"], "percentage": pct, "label": f"{t['name']} ({int(pct)}%)"})
    return out


def list_ledger_accounts():
    return _request("GET", "/ledger_accounts.json")


def get_revenue_ledger_account_id() -> str:
    """First revenue-type ledger account ID, cached in process."""
    global _revenue_ledger_id_cache
    if _revenue_ledger_id_cache:
        return _revenue_ledger_id_cache
    accounts = list_ledger_accounts() or []
    revenue = [a for a in accounts if a.get("account_type") == "revenue"]
    if not revenue:
        raise RuntimeError("No 'revenue'-type ledger account found in Moneybird.")
    _revenue_ledger_id_cache = revenue[0]["id"]
    return _revenue_ledger_id_cache


# ---- Sales invoices ----

def create_sales_invoice(payload: dict):
    """
    payload top-level fields:
      contact_id (required)
      invoice_date (YYYY-MM-DD, optional)
      due_date (YYYY-MM-DD, optional)
      reference (str, optional)
      details_attributes: list of dicts with
        { description, amount, price, tax_rate_id, ledger_account_id }
    """
    return _request("POST", "/sales_invoices.json", payload={"sales_invoice": payload})


def get_sales_invoice(invoice_id: str):
    return _request("GET", f"/sales_invoices/{invoice_id}.json")


def send_sales_invoice(invoice_id: str, delivery_method: str = "Manual",
                       email_address: str | None = None,
                       email_message: str | None = None):
    """
    delivery_method: 'Email' | 'Manual' | 'Post'.
    Manual = mark as sent without actually sending.
    Email  = send via Moneybird's email feature using the contact's
             send_invoices_to_email (or override with email_address).
    """
    body = {
        "sales_invoice_sending": {
            "delivery_method": delivery_method,
            "sending_scheduled": False,
            "deliver_ubl": False,
        }
    }
    if email_address:
        body["sales_invoice_sending"]["email_address"] = email_address
    if email_message:
        body["sales_invoice_sending"]["email_message"] = email_message
    return _request("PATCH", f"/sales_invoices/{invoice_id}/send_invoice.json", payload=body)


def delete_sales_invoice(invoice_id: str):
    """Delete a draft sales invoice. Only draft state is deletable."""
    return _request("DELETE", f"/sales_invoices/{invoice_id}.json")


def invoice_url(invoice_id: str) -> str:
    return f"https://moneybird.com/{ADMINISTRATION_ID}/sales_invoices/{invoice_id}"
