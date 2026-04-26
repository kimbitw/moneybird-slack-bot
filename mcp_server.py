"""
Remote MCP server exposing Slack + Moneybird sales-invoice tools to a
Claude Code Routine (Scheduled Agent).

Run:
    uvicorn mcp_server:app --host 0.0.0.0 --port $PORT

Auth: the value of MCP_AUTH_TOKEN is used as a URL path prefix. Anthropic's
Custom Connector dialog only exposes OAuth fields, so we use a "secret in
the URL" pattern (same as Slack incoming webhooks). The MCP endpoint is at
    /<MCP_AUTH_TOKEN>/mcp
Anyone who doesn't know the token gets a 404.

Env vars consumed (in addition to existing SLACK_BOT_TOKEN /
MONEYBIRD_TOKEN / MONEYBIRD_ADMINISTRATION_ID):
    MCP_AUTH_TOKEN  shared secret used as URL path prefix.
"""
import os
from typing import Any

from fastmcp import FastMCP
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

import moneybird_sales as mbs

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
MCP_AUTH_TOKEN = os.environ["MCP_AUTH_TOKEN"]

slack = WebClient(token=SLACK_BOT_TOKEN)

mcp = FastMCP("harro-invoice-mcp")


# =====================================================================
# Slack tools
# =====================================================================

@mcp.tool()
def slack_list_channel_messages(channel: str, oldest: str | None = None,
                                 limit: int = 50) -> list[dict]:
    """
    List top-level (parent) messages in a channel, newest first.
    `oldest` is a Slack ts (e.g. "1714000000.000000") to bound the search;
    omit to get the most recent `limit` messages.
    Each message includes ts, user, text, thread_ts (if thread exists),
    reply_count, and reactions [{name, count}].
    """
    kwargs: dict[str, Any] = {"channel": channel, "limit": limit}
    if oldest:
        kwargs["oldest"] = oldest
    resp = slack.conversations_history(**kwargs)
    out = []
    for m in resp.get("messages", []):
        if m.get("subtype") in ("channel_join", "channel_leave", "bot_add"):
            continue
        out.append({
            "ts": m.get("ts"),
            "user": m.get("user") or m.get("bot_id"),
            "text": m.get("text", ""),
            "thread_ts": m.get("thread_ts"),
            "reply_count": m.get("reply_count", 0),
            "reactions": [
                {"name": r["name"], "count": r["count"], "users": r.get("users", [])}
                for r in m.get("reactions", [])
            ],
        })
    return out


@mcp.tool()
def slack_get_thread_replies(channel: str, thread_ts: str) -> list[dict]:
    """
    Get all messages in a thread (parent + replies), oldest first.
    """
    resp = slack.conversations_replies(channel=channel, ts=thread_ts, limit=200)
    out = []
    for m in resp.get("messages", []):
        out.append({
            "ts": m.get("ts"),
            "user": m.get("user") or m.get("bot_id"),
            "text": m.get("text", ""),
            "is_bot": bool(m.get("bot_id")),
            "reactions": [
                {"name": r["name"], "count": r["count"]}
                for r in m.get("reactions", [])
            ],
        })
    return out


@mcp.tool()
def slack_post_thread_reply(channel: str, thread_ts: str, text: str) -> dict:
    """
    Post a plain-text reply in a thread.
    Returns {ok, ts, channel}.
    """
    resp = slack.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
    return {"ok": resp.get("ok"), "ts": resp.get("ts"), "channel": resp.get("channel")}


@mcp.tool()
def slack_post_message(channel: str, text: str) -> dict:
    """
    Post a top-level message to a channel (no thread). Use for status
    summaries; for normal request handling, use slack_post_thread_reply.
    """
    resp = slack.chat_postMessage(channel=channel, text=text)
    return {"ok": resp.get("ok"), "ts": resp.get("ts"), "channel": resp.get("channel")}


@mcp.tool()
def slack_add_reaction(channel: str, ts: str, name: str) -> dict:
    """
    Add an emoji reaction to a message. `name` without colons (e.g. 'eyes', 'memo', 'white_check_mark', 'x').
    Idempotent: ignores 'already_reacted' errors.
    """
    try:
        slack.reactions_add(channel=channel, timestamp=ts, name=name)
        return {"ok": True}
    except SlackApiError as e:
        if e.response.get("error") == "already_reacted":
            return {"ok": True, "note": "already_reacted"}
        raise


@mcp.tool()
def slack_remove_reaction(channel: str, ts: str, name: str) -> dict:
    """
    Remove an emoji reaction. Idempotent: ignores 'no_reaction' errors.
    """
    try:
        slack.reactions_remove(channel=channel, timestamp=ts, name=name)
        return {"ok": True}
    except SlackApiError as e:
        if e.response.get("error") == "no_reaction":
            return {"ok": True, "note": "no_reaction"}
        raise


@mcp.tool()
def slack_get_user_info(user_id: str) -> dict:
    """Get a user's display name and real name."""
    resp = slack.users_info(user=user_id)
    u = resp.get("user", {})
    profile = u.get("profile", {})
    return {
        "id": u.get("id"),
        "name": u.get("name"),
        "real_name": profile.get("real_name") or u.get("real_name"),
        "display_name": profile.get("display_name"),
    }


# =====================================================================
# Moneybird sales-invoice tools
# =====================================================================

@mcp.tool()
def mb_search_contact(query: str) -> list[dict]:
    """
    Search Moneybird contacts by name, email, etc. Returns up to 25 matches
    with id, label (display name + email), name, email,
    send_invoices_to_email.
    """
    raw = mbs.search_contacts(query=query) or []
    out = []
    for c in raw:
        name = (
            c.get("company_name")
            or " ".join([s for s in [c.get("firstname"), c.get("lastname")] if s])
            or "(unnamed)"
        )
        email = c.get("email") or c.get("send_invoices_to_email") or ""
        label = name + (f"  <{email}>" if email else "")
        out.append({
            "id": c.get("id"),
            "label": label,
            "name": name,
            "email": email,
            "send_invoices_to_email": c.get("send_invoices_to_email") or "",
            "country": c.get("country") or "",
            "tax_number": c.get("tax_number") or "",
        })
    return out


@mcp.tool()
def mb_create_contact(company_name: str | None = None,
                       firstname: str | None = None,
                       lastname: str | None = None,
                       email: str | None = None,
                       address1: str | None = None,
                       address2: str | None = None,
                       zipcode: str | None = None,
                       city: str | None = None,
                       country: str | None = None,
                       tax_number: str | None = None) -> dict:
    """
    Create a new Moneybird contact. At least one of company_name / firstname /
    lastname required. `email` is also stored as send_invoices_to_email
    (Moneybird's email-delivery destination).
    """
    if not (company_name or firstname or lastname):
        raise ValueError("At least one of company_name / firstname / lastname is required.")
    attrs: dict[str, Any] = {}
    for k, v in [
        ("company_name", company_name), ("firstname", firstname), ("lastname", lastname),
        ("address1", address1), ("address2", address2), ("zipcode", zipcode),
        ("city", city), ("country", country), ("tax_number", tax_number),
    ]:
        if v:
            attrs[k] = v
    if email:
        attrs["send_invoices_to_email"] = email
        attrs["send_estimates_to_email"] = email
    c = mbs.create_contact(attrs)
    name = (
        c.get("company_name")
        or " ".join([s for s in [c.get("firstname"), c.get("lastname")] if s])
        or "(unnamed)"
    )
    return {
        "id": c.get("id"),
        "name": name,
        "email": c.get("send_invoices_to_email") or "",
    }


@mcp.tool()
def mb_list_tax_rates() -> list[dict]:
    """
    List the sales-invoice tax rates available for selection.
    Filtered to the same set as the GAS UI: 9%, 21%, No-VAT (0%).
    Each item: { id, name, percentage, label }.
    """
    return mbs.list_sales_tax_rates_filtered()


@mcp.tool()
def mb_create_sales_invoice(contact_id: str,
                             details: list[dict],
                             invoice_date: str | None = None,
                             due_date: str | None = None,
                             reference: str | None = None) -> dict:
    """
    Create a sales invoice draft.

    `details` is a list of line items, each:
      {
        "description": str,
        "amount": str | int   # quantity, default "1"
        "price": str | float, # unit price excl. VAT
        "tax_rate_id": str,   # from mb_list_tax_rates
      }

    `invoice_date` / `due_date` are YYYY-MM-DD; if omitted, Moneybird uses
    its workflow defaults.

    Returns the created draft: { id, invoice_id (number, may be empty until
    issued), state, total_price_incl_tax, currency, url }.
    """
    if not details:
        raise ValueError("details must contain at least one line item.")
    ledger_id = mbs.get_revenue_ledger_account_id()
    payload: dict[str, Any] = {
        "contact_id": contact_id,
        "details_attributes": [],
    }
    if invoice_date:
        payload["invoice_date"] = invoice_date
    if due_date:
        payload["due_date"] = due_date
    if reference:
        payload["reference"] = reference
    for i, d in enumerate(details, start=1):
        if not d.get("description"):
            raise ValueError(f"details[{i}]: description is empty")
        if not d.get("tax_rate_id"):
            raise ValueError(f"details[{i}]: tax_rate_id is required")
        payload["details_attributes"].append({
            "description": d["description"],
            "amount": str(d.get("amount", "1")),
            "price": str(d.get("price", "0")),
            "tax_rate_id": d["tax_rate_id"],
            "ledger_account_id": ledger_id,
        })
    inv = mbs.create_sales_invoice(payload)
    return {
        "id": inv.get("id"),
        "invoice_id": inv.get("invoice_id") or "",
        "state": inv.get("state"),
        "total_price_incl_tax": inv.get("total_price_incl_tax"),
        "currency": inv.get("currency") or "EUR",
        "url": mbs.invoice_url(inv.get("id")),
    }


@mcp.tool()
def mb_send_sales_invoice(invoice_id: str, delivery_method: str = "Manual",
                           email_message: str | None = None) -> dict:
    """
    Issue a draft invoice (move it out of draft state).

    delivery_method:
      "Manual" — mark as sent without actually emailing (default).
      "Email"  — send via Moneybird's email using the contact's
                 send_invoices_to_email; uses Moneybird's default template.

    Returns the invoice after issuing: { id, invoice_id, state,
    total_price_incl_tax, currency, url }.
    """
    if delivery_method not in ("Manual", "Email", "Post"):
        raise ValueError("delivery_method must be 'Manual', 'Email', or 'Post'")
    inv = mbs.send_sales_invoice(invoice_id, delivery_method=delivery_method,
                                  email_message=email_message)
    return {
        "id": inv.get("id"),
        "invoice_id": inv.get("invoice_id") or "",
        "state": inv.get("state"),
        "total_price_incl_tax": inv.get("total_price_incl_tax"),
        "currency": inv.get("currency") or "EUR",
        "url": mbs.invoice_url(inv.get("id")),
    }


@mcp.tool()
def mb_get_sales_invoice(invoice_id: str) -> dict:
    """Fetch a sales invoice's current state."""
    inv = mbs.get_sales_invoice(invoice_id)
    return {
        "id": inv.get("id"),
        "invoice_id": inv.get("invoice_id") or "",
        "state": inv.get("state"),
        "total_price_incl_tax": inv.get("total_price_incl_tax"),
        "currency": inv.get("currency") or "EUR",
        "url": mbs.invoice_url(inv.get("id")),
    }


@mcp.tool()
def mb_delete_sales_invoice(invoice_id: str) -> dict:
    """
    Delete a draft sales invoice. Only works while state == 'draft'.
    Use this for the cancel flow.
    """
    mbs.delete_sales_invoice(invoice_id)
    return {"ok": True, "id": invoice_id}


# =====================================================================
# ASGI app — auth via "secret in URL path"
# =====================================================================

async def health(_request):
    return JSONResponse({"status": "ok"})


# Build the MCP HTTP app and mount it under /<MCP_AUTH_TOKEN>.
# The token-prefixed mount means: if you don't know the token, you get a
# 404 from Starlette's router. No middleware needed.
mcp_http_app = mcp.http_app(path="/mcp")

app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount(f"/{MCP_AUTH_TOKEN}", app=mcp_http_app),
    ],
    lifespan=mcp_http_app.lifespan,
)
