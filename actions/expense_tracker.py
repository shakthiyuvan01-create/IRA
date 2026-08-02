"""expense_tracker.py — AI-powered financial transaction extraction from emails.

Adapted from agentic-os-personal-main's server/ai/expenseExtractor.js.
Uses IRA's core.llm_client.chat() for AI extraction.
Stores transactions in the shared SQLite database.
"""

import re
from datetime import datetime
from core.data.database import get_db

CATEGORIES = [
    "Food", "Groceries", "Travel", "Shopping", "Bills", "Subscription",
    "Entertainment", "Health", "Salary", "Investment", "Transfer", "Other",
]

AMOUNT_RE = re.compile(r"(?:Rs\.?|INR|\$|USD)?\s?([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
TRANSACTION_WORDS = re.compile(
    r"(debited|spent|paid|purchase|payment|charged|withdrawn|order"
    r"|credited|received|refund|payout|salary|deposited|cashback"
    r"|transaction|invoice|receipt)",
    re.IGNORECASE,
)


def looks_transactional(msg):
    """Quick pre-check: skip messages with no monetary signal."""
    blob = " ".join(filter(None, [
        msg.get("subject", ""),
        msg.get("snippet", ""),
        msg.get("body", ""),
    ]))
    return bool(AMOUNT_RE.search(blob)) and bool(TRANSACTION_WORDS.search(blob))


def extract_transaction_from_email(msg):
    """Use AI to extract a transaction from an email message.

    Args:
        msg: dict with keys: subject, from_name, from_addr, body, snippet, internal_date

    Returns:
        dict with type, amount, currency, category, merchant, occurred_at
        or None if not a transaction
    """
    if not looks_transactional(msg):
        return None

    from core.llm_client import chat

    email_date = ""
    if msg.get("internal_date"):
        try:
            email_date = datetime.fromtimestamp(
                int(msg["internal_date"]) / 1000
            ).strftime("%Y-%m-%d")
        except Exception:
            email_date = datetime.now().strftime("%Y-%m-%d")
    else:
        email_date = datetime.now().strftime("%Y-%m-%d")

    body = (msg.get("body") or msg.get("snippet") or "")[:3000]
    user_content = (
        "Email date: " + email_date + "\n"
        "From: " + (msg.get("from_name") or "") + " <" + (msg.get("from_addr") or "") + ">\n"
        "Subject: " + (msg.get("subject") or "") + "\n\n"
        "Body:\n" + body
    )

    system = (
        "You extract financial transactions from emails. "
        "Respond with ONLY a JSON object:\n"
        '{"is_transaction": bool, "type": "expense" or "income", '
        '"amount": number, "currency": string, '
        '"category": one of ' + str(CATEGORIES) + ', '
        '"merchant": string, "occurred_at": "YYYY-MM-DD"}\n'
        "Rules:\n"
        "- is_transaction false for newsletters, promotions, OTPs\n"
        "- amount is numeric only (no currency symbols)\n"
        "- expense = money leaving, income = money arriving\n"
        "- Pick the closest category; use Other if unsure\n"
        "- occurred_at: transaction date if stated, else email date"
    )

    try:
        raw = chat(user_content, system=system, timeout=30)
    except Exception as e:
        print("[Expense] LLM error: " + str(e))
        return None

    # Parse JSON from the response
    import json
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON from the response
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            try:
                parsed = json.loads(raw[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                return None
        else:
            return None

    if not parsed.get("is_transaction"):
        return None

    amount = float(parsed.get("amount", 0))
    if amount <= 0:
        return None

    txn_type = parsed.get("type", "expense")
    if txn_type not in ("income", "expense"):
        txn_type = "expense"

    category = parsed.get("category", "Other")
    if category not in CATEGORIES:
        category = "Other"

    occurred = parsed.get("occurred_at", email_date)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", occurred):
        occurred = email_date

    return {
        "type": txn_type,
        "amount": amount,
        "currency": (parsed.get("currency", "INR") or "INR").upper()[:6],
        "category": category,
        "merchant": parsed.get("merchant") or msg.get("from_name"),
        "occurred_at": occurred,
    }


def sync_transactions_from_mail(limit=50):
    """Process all unprocessed mail messages and extract transactions.

    Returns:
        dict with scanned, extracted counts
    """
    db = get_db()
    messages = db.get_unprocessed_mail(limit=limit)
    if not messages:
        return {"scanned": 0, "extracted": 0}

    scanned = len(messages)
    extracted = 0

    for msg in messages:
        msg_dict = {
            "id": msg["id"],
            "subject": msg.get("subject", ""),
            "from_name": msg.get("from_name", ""),
            "from_addr": msg.get("from_addr", ""),
            "body": msg.get("body", ""),
            "snippet": msg.get("snippet", ""),
            "internal_date": msg.get("internal_date"),
        }

        txn = extract_transaction_from_email(msg_dict)
        if txn:
            db.add_expense(
                type_=txn["type"],
                amount=txn["amount"],
                currency=txn["currency"],
                category=txn["category"],
                merchant=txn["merchant"],
                occurred_at=txn["occurred_at"],
                source="gmail",
                message_id=msg["id"],
            )
            extracted += 1

        db.mark_mail_processed(msg["id"])

    return {"scanned": scanned, "extracted": extracted}


def expense_tracker(parameters=None, response=None, player=None, session_memory=None, speak=None):
    """Tool entry point — track expenses from email or list history."""
    from core.llm_client import chat

    params = parameters or {}
    action = params.get("action", "summary").strip().lower()
    days = int(params.get("days", 30))

    db = get_db()

    if action == "sync":
        if speak:
            speak("Scanning your email for transactions. One moment, Yuvan.")
        result = sync_transactions_from_mail()
        if result["extracted"] > 0:
            return (
                "Scanned " + str(result["scanned"]) + " emails and found "
                + str(result["extracted"]) + " transactions, Yuvan."
            )
        return "Scanned " + str(result["scanned"]) + " emails. No new transactions found, Yuvan."

    if action == "list":
        from datetime import datetime, timedelta
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        txns = db.list_expenses(from_date=from_date, limit=20)

        if not txns:
            return "No transactions found in the last " + str(days) + " days, Yuvan."

        lines = ["Your recent transactions, Yuvan:"]
        for t in txns:
            sign = "+" if t["type"] == "income" else "-"
            lines.append(
                sign + str(t["amount"]) + " " + t["currency"]
                + " | " + t["category"]
                + (" (" + t["merchant"] + ")" if t.get("merchant") else "")
                + " | " + (t["occurred_at"] or "")
            )
        return "\n".join(lines)

    # Default: summary
    from datetime import datetime, timedelta
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    summary = db.expense_summary(from_date=from_date)

    if summary["income"] == 0 and summary["expense"] == 0:
        return "No financial data found in the last " + str(days) + " days, Yuvan."

    return (
        "Financial summary for the last " + str(days) + " days, Yuvan:\n"
        "Income: " + str(summary["income"]) + "\n"
        "Expense: " + str(summary["expense"]) + "\n"
        "Net: " + str(summary["net"])
    )
