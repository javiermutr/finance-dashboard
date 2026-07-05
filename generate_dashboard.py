"""
Finance Dashboard Generator
Reads Notion Finance Tracker and generates an HTML dashboard.
Runs twice daily via GitHub Actions.
"""

import os
import json
import requests
from datetime import date, datetime
from typing import Optional
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Load .env if running locally ──────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed — rely on real env vars (GitHub Actions)

# ── Config ────────────────────────────────────────────────────────────────────
NOTION_TOKEN      = os.environ["NOTION_TOKEN"]
BUDGETS_DB        = "39d6673d-a868-4521-9acd-5e5543f4d705"
ACCOUNTS_DB       = "0f4234a1-4ebd-46c2-9ea7-4b2a990b19f7"
TRANSACTIONS_DB   = "e476092b-7600-4ff5-9a9e-8b97199fa096"
HEADERS         = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# Shared session with connection pooling. Re-using TCP/TLS connections instead
# of opening a new one per request is a meaningful speedup on its own, and is
# required for the thread pool below to actually run requests concurrently
# without each thread fighting over a single default-sized connection pool.
MAX_WORKERS = 12
_session = requests.Session()
_session.headers.update(HEADERS)
_adapter = requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
_session.mount("https://", _adapter)

# FX rates cache: fetched once per run from exchangerate-api (free tier)
_fx_cache: dict[str, float] = {}

# Account currency cache: account_page_id -> currency code (e.g. "EUR", "USD", "COP")
_account_currency_cache: dict[str, str] = {}

def get_account_currency(account_id: str) -> str:
    """Reads and caches the Currency select field from an Account page."""
    if account_id in _account_currency_cache:
        return _account_currency_cache[account_id]
    try:
        props = get_page_props(account_id)
        currency = get_select(props, "Currency") or "EUR"
    except Exception as e:
        print(f"  ⚠ Could not read currency for account {account_id}: {e}")
        currency = "EUR"
    _account_currency_cache[account_id] = currency
    return currency

MONTH_NAMES_ES = {
    1:"Enero",2:"Febrero",3:"Marzo",4:"Abril",5:"Mayo",6:"Junio",
    7:"Julio",8:"Agosto",9:"Septiembre",10:"Octubre",11:"Noviembre",12:"Diciembre"
}
MONTH_ABBR = {
    1:"Ene",2:"Feb",3:"Mar",4:"Abr",5:"May",6:"Jun",
    7:"Jul",8:"Ago",9:"Sep",10:"Oct",11:"Nov",12:"Dic"
}

# ── Connection check ──────────────────────────────────────────────────────────
def check_notion_connection() -> bool:
    """
    Verifies the Notion connection before running the full script.
    Prints a clear diagnosis if anything is wrong and exits early.
    Returns True if everything is OK.
    """
    print("🔌 Checking Notion connection...")

    # 1. Token present?
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        print("   ❌ NOTION_TOKEN is missing.")
        print("      → Make sure your .env file exists and contains NOTION_TOKEN=secret_...")
        return False
    if not token.startswith("secret_") and not token.startswith("ntn_"):
        print(f"   ⚠️  NOTION_TOKEN looks wrong (starts with '{token[:8]}...')")
        print("      → Token should start with 'secret_' or 'ntn_'")
        print("      → Get yours at: https://www.notion.so/my-integrations")
        return False

    # 2. Internet connectivity?
    try:
        requests.get("https://www.notion.com", timeout=5)
    except requests.exceptions.ConnectionError:
        print("   ❌ No internet connection.")
        print("      → Check your network and try again.")
        return False
    except requests.exceptions.Timeout:
        print("   ⚠️  Notion.com is slow to respond — continuing anyway.")

    # 3. Token valid? (call /users/me)
    try:
        r = requests.get(
            "https://api.notion.com/v1/users/me",
            headers=HEADERS,
            timeout=10
        )
        if r.status_code == 200:
            user = r.json()
            name = user.get("name") or user.get("bot", {}).get("owner", {}).get("user", {}).get("name", "unknown")
            print(f"   ✅ Connected as: {name}")
        elif r.status_code == 401:
            print("   ❌ Token is invalid or revoked (401 Unauthorized).")
            print("      → Go to https://www.notion.so/my-integrations")
            print("      → Check that the integration exists and copy the token again.")
            return False
        elif r.status_code == 403:
            print("   ❌ Token has no permissions (403 Forbidden).")
            print("      → Open your Finance Tracker in Notion")
            print("      → Click '...' → Connections → Add your integration")
            return False
        else:
            print(f"   ❌ Unexpected response from Notion API: {r.status_code}")
            print(f"      → {r.text[:200]}")
            return False
    except requests.exceptions.Timeout:
        print("   ❌ Notion API timed out.")
        print("      → Notion may be down. Check https://status.notion.com")
        return False
    except requests.exceptions.RequestException as e:
        print(f"   ❌ Request failed: {e}")
        return False

    # 4. Budgets DB accessible?
    try:
        r = requests.get(
            f"https://api.notion.com/v1/databases/{BUDGETS_DB}",
            headers=HEADERS,
            timeout=10
        )
        if r.status_code == 200:
            db_title = r.json().get("title", [{}])[0].get("plain_text", "Budgets DB")
            print(f"   ✅ Budgets database accessible: '{db_title}'")
        elif r.status_code == 404:
            print("   ❌ Budgets database not found (404).")
            print(f"      → DB ID in script: {BUDGETS_DB}")
            print("      → Make sure the integration has access to this database.")
            return False
        elif r.status_code == 403:
            print("   ❌ No access to Budgets database (403).")
            print("      → Open the Budgets page in Notion → '...' → Connections → Add integration.")
            return False
        else:
            print(f"   ⚠️  Unexpected status for Budgets DB: {r.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"   ❌ Could not reach Budgets DB: {e}")
        return False

    print("   ✅ All checks passed — ready to generate dashboard.\n")
    return True


# ── Notion helpers ─────────────────────────────────────────────────────────────
import time

def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """
    Wraps a session request with retry-on-429 handling. Notion's API rate
    limit is roughly 3 requests/second average; running MAX_WORKERS requests
    concurrently can occasionally trip it. Honor the Retry-After header if
    present, otherwise back off with a short fixed delay, up to 5 attempts.
    """
    for attempt in range(5):
        r = _session.request(method, url, **kwargs)
        if r.status_code != 429:
            return r
        wait = float(r.headers.get("Retry-After", 1.0))
        print(f"    ⏳ Rate limited (429) — waiting {wait:.1f}s before retry {attempt + 1}/5...")
        time.sleep(wait)
    return r  # give up after 5 attempts, let raise_for_status() report it

def notion_get(path: str) -> dict:
    r = _request_with_retry("GET", f"https://api.notion.com/v1/{path}")
    r.raise_for_status()
    return r.json()

def notion_post(path: str, body: dict) -> dict:
    r = _request_with_retry("POST", f"https://api.notion.com/v1/{path}", json=body)
    r.raise_for_status()
    return r.json()

def get_page(page_id: str) -> dict:
    return notion_get(f"pages/{page_id.replace('-','')}")

_page_cache: dict[str, dict] = {}

def get_page_props(page_id: str) -> dict:
    """Cached page property lookup — avoids refetching the same page twice
    in a single run (e.g. an account looked up both for currency and balance)."""
    if page_id not in _page_cache:
        _page_cache[page_id] = get_page(page_id)["properties"]
    return _page_cache[page_id]

def get_num(props: dict, key: str) -> float:
    v = props.get(key, {})
    if v.get("type") == "number":
        return v.get("number") or 0.0
    return 0.0

def get_relations(props: dict, key: str) -> list[str]:
    v = props.get(key, {})
    if v.get("type") == "relation":
        return [r["id"] for r in v.get("relation", [])]
    return []

def get_title(props: dict) -> str:
    for v in props.values():
        if v.get("type") == "title":
            parts = v.get("title", [])
            return "".join(p.get("plain_text","") for p in parts)
    return ""

def get_select(props: dict, key: str) -> str:
    v = props.get(key, {})
    if v.get("type") == "select" and v.get("select"):
        return v["select"].get("name","")
    return ""

# ── FX conversion ──────────────────────────────────────────────────────────────
def eur_rate_for(currency: str, tx_date: str) -> float:
    """Returns how many EUR = 1 unit of currency on tx_date (approx)."""
    if currency == "EUR":
        return 1.0
    key = f"{currency}_{tx_date}"
    if key in _fx_cache:
        return _fx_cache[key]
    # Use exchangerate.host free historical API (no key needed)
    try:
        url = f"https://api.exchangerate.host/convert?from={currency}&to=EUR&date={tx_date}&amount=1"
        r = requests.get(url, timeout=8)
        data = r.json()
        rate = data.get("result") or data.get("info", {}).get("rate", None)
        if rate:
            _fx_cache[key] = float(rate)
            return float(rate)
    except Exception:
        pass
    # Fallback hardcoded rates (approximate June 2026)
    fallbacks = {"USD": 0.921, "COP": 0.000244, "GBP": 1.168, "CHF": 1.098}
    return fallbacks.get(currency, 1.0)

def to_eur(amount: float, currency: str, tx_date: str) -> float:
    if currency == "EUR":
        return amount
    return amount * eur_rate_for(currency, tx_date)

# ── Transaction reader ─────────────────────────────────────────────────────────
def read_transaction(tx_id: str) -> dict:
    """Returns {amount_eur, type, date, name, currency}"""
    props = get_page_props(tx_id)
    raw_amount = get_num(props, "Amount")
    tx_date_prop = props.get("date:Date:start") or props.get("Date", {})
    # Handle different date formats from Notion API
    if isinstance(tx_date_prop, str):
        tx_date = tx_date_prop[:10]
    elif isinstance(tx_date_prop, dict):
        d = tx_date_prop.get("date", {}) or {}
        tx_date = (d.get("start") or "2026-06-01")[:10]
    else:
        tx_date = "2026-06-01"

    # Currency: read from the linked Account, NOT from a "Currency" field on the
    # transaction itself (Transactions DB has no real Currency select — it was
    # silently defaulting to EUR for every transaction, which inflated COP/USD
    # amounts massively, e.g. 25,541.92 COP being treated as 25,541.92 EUR).
    account_ids = get_relations(props, "Account")
    if account_ids:
        currency = get_account_currency(account_ids[0])
    else:
        currency = "EUR"
        print(f"  ⚠ Transaction {tx_id} has no linked Account — defaulting to EUR")

    tx_type = get_select(props, "Type")
    name    = get_title(props)
    amount_eur = to_eur(raw_amount, currency, tx_date)
    if currency != "EUR":
        print(f"    💱 FX: {raw_amount} {currency} -> €{amount_eur:.2f}  ({name}, {tx_date})")
    return {"amount_eur": amount_eur, "type": tx_type, "date": tx_date, "name": name, "currency": currency}

# ── Budget reader ──────────────────────────────────────────────────────────────
def read_budget_page(page_id: str) -> dict:
    """Returns {name, limit, transactions:[{amount_eur,...}], invest_total, category_type}"""
    props = get_page_props(page_id)
    name  = get_title(props)
    limit = get_num(props, "Limit")
    tx_ids = get_relations(props, "Linked Transactions")

    # Fetch all linked transactions CONCURRENTLY instead of one at a time.
    # This is the main bottleneck in the whole script — a month with 30+
    # transactions across a dozen budgets used to mean 300+ sequential HTTP
    # round-trips. Reading each budget's transactions in parallel (and budgets
    # themselves in parallel too, see query_budgets) cuts wall-clock time
    # roughly by a factor of MAX_WORKERS, network limits permitting.
    raw_results: dict[str, Optional[dict]] = {}
    raw_errors: dict[str, Exception] = {}
    if tx_ids:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_to_id = {pool.submit(read_transaction, tx_id): tx_id for tx_id in tx_ids}
            for future in as_completed(future_to_id):
                tx_id = future_to_id[future]
                try:
                    raw_results[tx_id] = future.result()
                except Exception as e:
                    raw_errors[tx_id] = e

    transactions = []
    invest_total = 0.0
    # Iterate in original tx_ids order so console output stays stable/readable
    # across runs, even though the underlying fetches happened concurrently.
    for tx_id in tx_ids:
        if tx_id in raw_errors:
            print(f"  ⚠ Error reading tx {tx_id}: {raw_errors[tx_id]}")
            continue
        tx = raw_results.get(tx_id)
        if tx is None:
            continue
        if tx["type"] == "Profits":
            # Safety net: Profits transactions represent market revaluation
            # (e.g. Trade Republic portfolio gains), not real income or spend.
            # They should normally never be linked to a Budget at all, but if
            # one ever is by mistake, exclude it here so it can't inflate or
            # deflate Income/Expenses/Savings Rate.
            print(f"    ⏭  Skipping Profits transaction in budget calc: {tx['name']} (€{tx['amount_eur']:.2f})")
            continue
        if tx["type"] == "Invest":
            # "Invest" = money moving from a liquid account into an
            # investment account (the outgoing side; the matching incoming
            # side is a Transfer In on the destination account). This is a
            # SAVINGS event, not a category expense — track it separately
            # so it never inflates whatever Budget it happens to be linked
            # to, and gets added directly to Savings in classify().
            invest_total += abs(tx["amount_eur"])
            print(f"    💸 Invest transaction tracked separately: {tx['name']} (€{tx['amount_eur']:.2f})")
            continue
        transactions.append(tx)

    total_spent = sum(t["amount_eur"] for t in transactions)
    return {
        "name": name,
        "limit": limit,
        "invest_total": invest_total,
        "spent": total_spent,
        "transactions": transactions,
        "tx_count": len(transactions),
    }

# ── Discover all months/years present in the Budgets DB ────────────────────────
def discover_all_periods() -> list[tuple[str, int]]:
    """
    Scans the entire Budgets DB and returns a sorted list of unique (month_abbr, year)
    tuples actually present, oldest first. Used to read all available history,
    not just the current month.
    """
    all_periods: set[tuple[str, int]] = set()
    start_cursor = None

    while True:
        body: dict = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        data = notion_post(f"databases/{BUDGETS_DB}/query", body)
        for p in data.get("results", []):
            props = p.get("properties", {})
            month_prop = props.get("Month", {})
            year_prop  = props.get("Year", {})
            month_val  = (month_prop.get("select") or {}).get("name") if month_prop.get("type") == "select" else None
            year_val   = year_prop.get("number") if year_prop.get("type") == "number" else None
            if month_val and year_val:
                all_periods.add((month_val, int(year_val)))

        if data.get("has_more"):
            start_cursor = data.get("next_cursor")
        else:
            break

    # Sort chronologically using MONTH_ABBR ordering
    abbr_order = {v: k for k, v in MONTH_ABBR.items()}
    sorted_periods = sorted(all_periods, key=lambda t: (t[1], abbr_order.get(t[0], 0)))
    return sorted_periods

# ── Profits query ──────────────────────────────────────────────────────────────
def query_profits(month_abbr: str, year: int) -> float:
    """
    Reads all transactions with Type = 'Profits' for the given month/year
    directly from the Transactions DB (they are NOT linked to any Budget).
    Returns the total in EUR, applying FX conversion per transaction date.
    """
    # Build date range for the month
    month_num = next(num for num, a in MONTH_ABBR.items() if a == month_abbr)
    import calendar
    last_day = calendar.monthrange(year, month_num)[1]
    date_start = f"{year}-{month_num:02d}-01"
    date_end   = f"{year}-{month_num:02d}-{last_day:02d}"

    body = {
        "filter": {
            "and": [
                {"property": "Type",        "select":  {"equals": "Profits"}},
                {"property": "Date",        "date":    {"on_or_after":  date_start}},
                {"property": "Date",        "date":    {"on_or_before": date_end}},
            ]
        },
        "page_size": 100,
    }

    total_eur = 0.0
    tx_count  = 0
    start_cursor = None

    while True:
        if start_cursor:
            body["start_cursor"] = start_cursor
        data = notion_post(f"databases/{TRANSACTIONS_DB}/query", body)

        futures_map = {}
        pages = data.get("results", [])
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for page in pages:
                futures_map[pool.submit(read_transaction, page["id"])] = page["id"]

        for future in as_completed(futures_map):
            try:
                tx = future.result()
                total_eur += tx["amount_eur"]
                tx_count  += 1
                print(f"    📈 Profits: {tx['name']:40s}  {tx['currency']:4s} {tx['amount_eur']:10.2f}  ({tx['date']})")
            except Exception as e:
                print(f"    ⚠ Error reading Profits tx: {e}")

        if data.get("has_more"):
            start_cursor = data.get("next_cursor")
        else:
            break

    print(f"   ✅ Total Profits {month_abbr} {year}: €{total_eur:,.2f}  ({tx_count} transactions)\n")
    return round(total_eur, 2)

def query_savings(month_abbr: str, year: int) -> float:
    """
    Reads all transactions with Type = 'Invest' for the given month/year
    directly from the Transactions DB.
    This is the RELIABLE way to calculate Savings — based on transaction Type,
    not on fragile Budget name matching.
    Returns the total in EUR, applying FX conversion per transaction date.
    """
    month_num = next(num for num, a in MONTH_ABBR.items() if a == month_abbr)
    import calendar
    last_day = calendar.monthrange(year, month_num)[1]
    date_start = f"{year}-{month_num:02d}-01"
    date_end   = f"{year}-{month_num:02d}-{last_day:02d}"

    body = {
        "filter": {
            "and": [
                {"property": "Type", "select": {"equals": "Invest"}},
                {"property": "Date", "date":   {"on_or_after":  date_start}},
                {"property": "Date", "date":   {"on_or_before": date_end}},
            ]
        },
        "page_size": 100,
    }

    total_eur = 0.0
    tx_count  = 0
    start_cursor = None

    while True:
        if start_cursor:
            body["start_cursor"] = start_cursor
        data = notion_post(f"databases/{TRANSACTIONS_DB}/query", body)

        futures_map = {}
        pages = data.get("results", [])
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for page in pages:
                futures_map[pool.submit(read_transaction, page["id"])] = page["id"]

        for future in as_completed(futures_map):
            try:
                tx = future.result()
                total_eur += abs(tx["amount_eur"])
                tx_count  += 1
                print(f"    💰 Invest: {tx['name']:40s}  {tx['currency']:4s} {abs(tx['amount_eur']):10.2f}  ({tx['date']})")
            except Exception as e:
                print(f"    ⚠ Error reading Invest tx: {e}")

        if data.get("has_more"):
            start_cursor = data.get("next_cursor")
        else:
            break

    print(f"   ✅ Total Savings (Invest) {month_abbr} {year}: €{total_eur:,.2f}  ({tx_count} transactions)\n")
    return round(total_eur, 2)

# ── Net Worth calculation ────────────────────────────────────────────────────────
def calculate_base_net_worth() -> float:
    """
    Sums the INITIAL BALANCE of every Account in the Accounts DB, excluding
    Type = 'Deposit Flat', converted to EUR using today's FX rate.

    This is the FIXED starting point for the whole Net Worth model:

        NetWorth(month) = base_net_worth
                         + cumulative_income(up to and including month)
                         - cumulative_expenses(up to and including month)

    Each month's margin (income - expenses) accumulates on top of this base,
    so Net Worth grows/shrinks month over month purely from your tracked
    cash flow — exactly mirroring how the accounts would actually move if
    every euro in/out were captured as a transaction.
    """
    print("💰 Calculating base Net Worth (sum of Initial Balances) from Accounts DB...")
    total_eur = 0.0
    start_cursor = None
    accounts_read = 0
    accounts_skipped = 0
    today_str = date.today().isoformat()

    while True:
        body: dict = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        data = notion_post(f"databases/{ACCOUNTS_DB}/query", body)

        for page in data.get("results", []):
            props = page.get("properties", {})
            acc_type = get_select(props, "Type")
            acc_name = get_title(props)

            if acc_type == "Deposit Flat":
                accounts_skipped += 1
                continue

            currency = get_select(props, "Currency") or "EUR"
            initial  = get_num(props, "Initial Balance")
            initial_eur = to_eur(initial, currency, today_str)

            print(f"    {acc_name:30s}  {currency:4s} {initial:12,.2f}  ->  €{initial_eur:10,.2f}")
            total_eur += initial_eur
            accounts_read += 1

        if data.get("has_more"):
            start_cursor = data.get("next_cursor")
        else:
            break

    print(f"   ✅ Summed Initial Balance of {accounts_read} accounts (skipped {accounts_skipped} Deposit Flat)")
    print(f"   💶 Base Net Worth: €{total_eur:,.2f}\n")
    return round(total_eur, 2)

# ── Query all budgets for a given month/year ───────────────────────────────────
def query_budgets(month_abbr: str, year: int) -> list[dict]:
    """Searches the Budgets DB for all pages matching month+year."""
    body = {
        "filter": {
            "and": [
                {"property": "Month", "select": {"equals": month_abbr}},
                {"property": "Year",  "number": {"equals": year}},
            ]
        },
        "page_size": 100,
    }
    data = notion_post(f"databases/{BUDGETS_DB}/query", body)
    pages = data.get("results", [])
    print(f"  Found {len(pages)} budget pages for {month_abbr} {year}")

    budgets = []
    for p in pages:
        try:
            b = read_budget_page(p["id"])
            invest_note = f"  invest={b['invest_total']:.2f}" if b['invest_total'] > 0 else ""
            print(f"    ✓ {b['name']:40s} limit={b['limit']:8.2f}  spent={b['spent']:8.2f}  ({b['tx_count']} tx){invest_note}")
            budgets.append(b)
        except Exception as e:
            print(f"    ✗ Error reading budget {p['id']}: {e}")
    return budgets

# ── Financial calculations ──────────────────────────────────────────────────────
# Categories that count toward Income (excluded from expenses)
INCOME_CATS  = {"salary", "other income"}
# Categories that count toward Savings AND should be hidden from the expense tab
# (pure investment flows — money moving to brokerage accounts)
SAVINGS_ONLY_CATS = {"savings & investment"}
# Categories that count toward Savings BUT should also appear in the expense tab
# (mandatory recurring contributions with a budget limit that can be exceeded)
SAVINGS_AND_VISIBLE_CATS = {"pension"}
SAVINGS_CATS = SAVINGS_ONLY_CATS | SAVINGS_AND_VISIBLE_CATS  # kept for compat

def classify(budgets: list[dict], savings_override: float = 0.0) -> dict:
    """
    Classifies budgets into Income, Savings, and Expenses.

    savings_override: total Savings passed in from query_savings() — computed
    directly from Type='Invest' transactions in the Transactions DB. This is
    the reliable source.

    Key design decisions:
    - "Savings & Investment" budgets → count as Savings, hidden from expense tab
    - "Pension" budgets → count as Savings AND appear in expense tab (so over-budget
      is visible), since Pension is a mandatory recurring payment with a limit
    - Income budgets → never appear in expense tab
    """
    income   = 0.0
    savings  = savings_override  # start with the reliable Invest-type total
    expenses = []

    for b in budgets:
        cat_key = b["name"].lower()
        # Strip month suffix like " - Jun 2026"
        for sep in [" - jun", " - jul", " - ago", " - sep", " - oct", " - nov", " - dic",
                    " - jan", " - feb", " - mar", " - apr", " - may"]:
            if sep in cat_key:
                cat_key = cat_key.split(sep)[0].strip()
                break

        if any(ic in cat_key for ic in INCOME_CATS):
            # Income: add to income total, never shown in expense tab
            income += b["spent"]
        elif any(sc in cat_key for sc in SAVINGS_ONLY_CATS):
            # Pure savings/invest: count toward savings, hidden from expense tab
            # (avoid double-counting with invest_total from query_savings)
            remaining = b["spent"] - b.get("invest_total", 0.0)
            if remaining > 0:
                savings += remaining
        elif any(sc in cat_key for sc in SAVINGS_AND_VISIBLE_CATS):
            # Pension: count toward savings AND show in expense tab
            # so over-budget alerts are visible
            remaining = b["spent"] - b.get("invest_total", 0.0)
            if remaining > 0:
                savings += remaining
            expenses.append(b)  # also show in dashboard budget tab
        else:
            expenses.append(b)

    total_expenses = sum(e["spent"] for e in expenses)
    sr = (savings / income * 100) if income > 0 else 0.0

    return {
        "income": income,
        "savings": savings,
        "expenses": expenses,
        "total_expenses": total_expenses,
        "savings_rate": sr,
        "margin": income - total_expenses,
    }

# ── HTML generation ────────────────────────────────────────────────────────────
def clean_cat_name(name: str) -> str:
    """Strip month suffix like ' - Jun 2026' from a budget/category name."""
    for sep in [" - jun", " - jul", " - ago", " - sep", " - oct", " - nov", " - dic",
                " - jan", " - feb", " - mar", " - apr", " - may"]:
        if sep in name.lower():
            return name[:name.lower().index(sep)].strip()
    return name

def build_budget_js(expenses: list[dict]) -> str:
    """Generate a compact JS array literal (as a Python list of dicts, then json.dumps'd by caller).
    Each category includes its individual transactions (name, amount, date) so the
    dashboard can show a transaction-level breakdown when a category is clicked."""
    out = []
    for e in expenses:
        name = clean_cat_name(e["name"])
        s = round(e["spent"], 2)
        l = round(e["limit"], 2)
        p = bool(l <= 1 and s > 0)
        tx_list = sorted(
            (
                {"n": t["name"], "a": round(t["amount_eur"], 2), "d": t["date"]}
                for t in e.get("transactions", [])
            ),
            key=lambda t: t["d"],
            reverse=True,
        )
        out.append({"cat": name, "s": s, "l": l, "p": p, "tx": tx_list})
    return out

def generate_html(months_data: list[dict], latest_idx: int) -> str:
    """
    Render the full dashboard HTML with REAL month-to-month navigation.

    months_data: list of dicts, one per month (chronological order), each with:
        { label, abbr, year, income, savings, total_expenses, savings_rate,
          margin, net_worth, profits, budgets (list of {cat,s,l,p,tx}) }
    latest_idx: index into months_data that should be shown by default (usually last)
    """
    try:
        import zoneinfo
        berlin = zoneinfo.ZoneInfo("Europe/Berlin")
        now_str = datetime.now(tz=berlin).strftime("%d %b %Y %H:%M")
    except Exception:
        # Fallback for Python < 3.9
        now_str = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")

    months_json = json.dumps(months_data, ensure_ascii=False)

    chart_window  = months_data[-6:]
    hist_labels   = json.dumps([f"{h['abbr']} {h['year']}" for h in chart_window])
    hist_nw       = json.dumps([h["net_worth"] for h in chart_window])
    hist_income   = json.dumps([h["income"] for h in chart_window])
    hist_exp      = json.dumps([h["total_expenses"] for h in chart_window])
    hist_savings  = json.dumps([h["savings"] for h in chart_window])
    hist_margin   = json.dumps([round(h["margin"], 2) for h in chart_window])
    margin_colors = json.dumps(["rgba(29,158,117,0.85)" if h["margin"] >= 0 else "rgba(216,90,48,0.85)" for h in chart_window])
    margin_borders = json.dumps(["#1D9E75" if h["margin"] >= 0 else "#D85A30" for h in chart_window])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Finance Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif;background:#0D1B2A;color:#E8EDF2;padding:20px;}}
  .dash{{max-width:980px;margin:0 auto;}}

  /* ── Top bar ── */
  .top-bar{{display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem;flex-wrap:wrap;gap:10px;}}
  .top-bar h1{{font-size:18px;font-weight:600;}}
  .notion-badge{{font-size:10px;background:#1a3d2e;color:#1D9E75;padding:2px 8px;border-radius:20px;font-weight:700;margin-left:8px;}}
  .updated-badge{{font-size:10px;color:#8899AA;margin-left:6px;}}
  .month-nav{{display:flex;align-items:center;gap:8px;}}
  .month-nav button{{background:#1a2a3a;border:0.5px solid #2a3a4a;border-radius:8px;padding:4px 12px;cursor:pointer;color:#E8EDF2;font-size:13px;}}
  .month-nav button:hover:not(:disabled){{background:#2a3a4a;}}
  .month-nav button:disabled{{opacity:0.35;cursor:not-allowed;}}
  .month-label{{font-size:14px;font-weight:600;min-width:100px;text-align:center;}}
  .month-pos{{font-size:10px;color:#8899AA;min-width:32px;text-align:center;}}

  /* ── Alert banner ── */
  .alert-banner{{display:none;background:#1a0a0a;border:0.5px solid #D85A30;border-radius:10px;padding:10px 14px;margin-bottom:10px;font-size:12px;color:#D85A30;font-weight:500;}}
  .alert-banner.visible{{display:block;}}

  /* ── KPI grid ── */
  .kpi-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.25rem;}}
  .kpi{{background:#142030;border-radius:12px;padding:16px;border-left:3px solid transparent;}}
  .kpi.profits  {{border-left-color:#534AB7;}}
  .kpi.income   {{border-left-color:#1D9E75;}}
  .kpi.expenses {{border-left-color:#D85A30;}}
  .kpi.savings  {{border-left-color:#BA7517;}}
  .kpi-label{{font-size:10px;color:#8899AA;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px;}}
  .kpi-value{{font-size:24px;font-weight:700;}}
  .kpi-sub{{font-size:11px;color:#8899AA;margin-top:3px;}}
  .kpi-trend{{font-size:11px;margin-top:4px;font-weight:600;}}
  .kpi-badge{{display:inline-block;font-size:11px;padding:2px 7px;border-radius:20px;margin-top:5px;font-weight:500;}}
  .badge-up  {{background:#0f2a1e;color:#1D9E75;}}
  .badge-down{{background:#2a1010;color:#D85A30;}}
  .badge-warn{{background:#2a1e08;color:#BA7517;}}
  .badge-ok  {{background:#1a1830;color:#8B83FF;}}
  .trend-up  {{color:#1D9E75;}}
  .trend-down{{color:#D85A30;}}
  .trend-flat{{color:#8899AA;}}

  /* ── Charts ── */
  .charts-row{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;}}
  .chart-card{{background:#0f1e2d;border:0.5px solid #1e2e3e;border-radius:12px;padding:16px;margin-bottom:12px;}}
  .chart-title{{font-size:11px;font-weight:600;color:#8899AA;margin-bottom:12px;text-transform:uppercase;letter-spacing:0.05em;}}
  .legend-row{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:10px;font-size:12px;color:#8899AA;}}
  .legend-dot{{width:10px;height:10px;border-radius:2px;display:inline-block;margin-right:4px;}}

  /* ── Savings Rate semi-donut gauge ── */
  .gauge-wrap{{display:flex;flex-direction:column;align-items:center;padding:8px 0 4px;}}
  .gauge-svg-wrap{{position:relative;width:200px;height:110px;}}
  .gauge-svg-wrap svg{{width:200px;height:110px;}}
  .gauge-center-text{{position:absolute;bottom:4px;left:50%;transform:translateX(-50%);text-align:center;}}
  .gauge-big{{font-size:28px;font-weight:700;line-height:1;}}
  .gauge-small{{font-size:11px;color:#8899AA;margin-top:2px;}}
  .gauge-labels-row{{display:flex;justify-content:space-between;width:200px;font-size:10px;color:#8899AA;margin-top:4px;}}
  .gauge-goal-label{{font-size:12px;color:#8899AA;margin-top:6px;}}

  /* ── Expenses tabs ── */
  .tabs-header{{display:flex;margin-bottom:14px;border-bottom:0.5px solid #1e2e3e;}}
  .tab-btn{{padding:7px 16px;font-size:12px;font-weight:600;cursor:pointer;background:none;border:none;border-bottom:2px solid transparent;color:#8899AA;margin-bottom:-1px;}}
  .tab-btn.active{{color:#E8EDF2;border-bottom-color:#534AB7;}}
  .tab-pane{{display:none;}}
  .tab-pane.active{{display:block;}}
  .section-label{{font-size:10px;text-transform:uppercase;letter-spacing:0.06em;margin:10px 0 8px;font-weight:600;}}

  /* ── Budget rows ── */
  .spend-item{{margin-bottom:12px;cursor:pointer;padding:8px;margin:-8px -8px 4px -8px;border-radius:8px;transition:background 0.15s;}}
  .spend-item:hover{{background:#142030;}}
  .spend-header{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px;}}
  .spend-cat{{font-size:12px;font-weight:600;}}
  .spend-amounts{{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px;}}
  .spend-used{{font-size:13px;font-weight:700;}}
  .spend-budget{{font-size:11px;color:#8899AA;}}
  .spend-track{{width:100%;height:7px;background:#142030;border-radius:4px;overflow:hidden;}}
  .spend-fill{{height:100%;border-radius:4px;}}
  .no-lim-row{{margin-bottom:8px;display:flex;align-items:center;gap:8px;}}
  .no-lim-cat{{font-size:12px;color:#8899AA;width:150px;flex-shrink:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
  .no-lim-track{{flex:1;height:6px;background:#142030;border-radius:4px;}}
  .no-lim-amt{{font-size:11px;color:#8899AA;width:60px;text-align:right;flex-shrink:0;}}

  /* ── Distribution rows ── */
  .dist-row{{display:flex;align-items:center;gap:10px;margin-bottom:9px;cursor:pointer;padding:6px 8px;margin-left:-8px;margin-right:-8px;border-radius:8px;transition:background 0.15s;}}
  .dist-row:hover{{background:#142030;}}
  .dist-dot{{width:10px;height:10px;border-radius:2px;flex-shrink:0;}}
  .dist-cat{{font-size:12px;width:115px;flex-shrink:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
  .dist-track{{flex:1;height:7px;background:#142030;border-radius:4px;overflow:hidden;}}
  .dist-fill{{height:100%;border-radius:4px;}}
  .dist-pct{{font-size:11px;color:#8899AA;width:34px;text-align:right;flex-shrink:0;}}
  .dist-inc{{font-size:10px;color:#534AB7;width:38px;text-align:right;flex-shrink:0;}}
  .dist-amt{{font-size:11px;color:#8899AA;width:56px;text-align:right;flex-shrink:0;}}

  /* ── Modal ── */
  .modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:100;align-items:flex-start;justify-content:center;padding:8vh 16px;overflow-y:auto;}}
  .modal-overlay.open{{display:flex;}}
  .modal-panel{{background:#0f1e2d;border:0.5px solid #2a3a4a;border-radius:14px;padding:20px;width:100%;max-width:480px;max-height:76vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,0.5);}}
  .modal-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px;}}
  .modal-title{{font-size:15px;font-weight:700;}}
  .modal-close{{background:#1a2a3a;border:none;color:#8899AA;width:26px;height:26px;border-radius:7px;cursor:pointer;font-size:13px;line-height:1;}}
  .modal-close:hover{{background:#2a3a4a;color:#E8EDF2;}}
  .modal-sub{{font-size:11px;color:#8899AA;margin-bottom:14px;}}
  .modal-tx-list{{overflow-y:auto;}}
  .modal-tx-row{{display:flex;justify-content:space-between;align-items:baseline;padding:9px 0;border-bottom:0.5px solid #1e2e3e;gap:12px;}}
  .modal-tx-row:last-child{{border-bottom:none;}}
  .modal-tx-name{{font-size:12px;color:#E8EDF2;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
  .modal-tx-date{{font-size:10px;color:#8899AA;flex-shrink:0;}}
  .modal-tx-amt{{font-size:12px;font-weight:700;flex-shrink:0;min-width:64px;text-align:right;}}
  .modal-tx-empty{{font-size:12px;color:#8899AA;text-align:center;padding:24px 0;font-style:italic;}}

  .no-month-data{{font-size:12px;color:#8899AA;text-align:center;padding:30px 0;font-style:italic;}}
  .placeholder-note{{font-size:10px;color:#BA7517;}}
  @media(max-width:600px){{.kpi-grid{{grid-template-columns:1fr 1fr;}}.charts-row{{grid-template-columns:1fr;}}}}
</style>
</head>
<body>
<div class="dash">

  <div class="top-bar">
    <h1>💰 Finance Dashboard<span class="notion-badge">● NOTION LIVE</span><span class="updated-badge">{now_str}</span></h1>
    <div class="month-nav">
      <button id="btnPrev" onclick="changeMonth(-1)">‹</button>
      <span class="month-label" id="monthLabel"></span>
      <span class="month-pos" id="monthPos"></span>
      <button id="btnNext" onclick="changeMonth(1)">›</button>
    </div>
  </div>

  <div class="alert-banner" id="alertBanner"></div>

  <div class="kpi-grid">
    <div class="kpi profits">
      <div class="kpi-label">Market Profits</div>
      <div class="kpi-value" id="kpi-nw"></div>
      <div class="kpi-sub">Revaluation this month</div>
      <div class="kpi-trend" id="kpi-nw-trend"></div>
    </div>
    <div class="kpi income">
      <div class="kpi-label">Income</div>
      <div class="kpi-value" id="kpi-inc"></div>
      <div class="kpi-sub">Salary + Other Income</div>
      <div class="kpi-trend" id="kpi-inc-trend"></div>
    </div>
    <div class="kpi expenses">
      <div class="kpi-label">Expenses</div>
      <div class="kpi-value" id="kpi-exp"></div>
      <div class="kpi-sub" id="kpi-exp-pct"></div>
      <div class="kpi-trend" id="kpi-exp-trend"></div>
    </div>
    <div class="kpi savings">
      <div class="kpi-label">Savings Rate</div>
      <div class="kpi-value" id="kpi-sr"></div>
      <div class="kpi-sub" id="kpi-sr-eur"></div>
      <div class="kpi-badge" id="kpi-sr-badge"></div>
    </div>
  </div>

  <div class="charts-row">
    <div class="chart-card" style="margin-bottom:0;">
      <div class="chart-title">Net Worth — last 6 months</div>
      <div style="position:relative;height:180px;"><canvas id="nwChart"></canvas></div>
    </div>
    <div class="chart-card" style="margin-bottom:0;">
      <div class="chart-title">Monthly Cash Flow</div>
      <div class="legend-row">
        <span><span class="legend-dot" style="background:#1D9E75"></span>Income</span>
        <span><span class="legend-dot" style="background:#D85A30"></span>Expenses</span>
        <span><span class="legend-dot" style="background:#BA7517"></span>Savings</span>
      </div>
      <div style="position:relative;height:155px;"><canvas id="sumChart"></canvas></div>
    </div>
  </div>

  <div class="chart-card">
    <div class="chart-title">Monthly Margin (Income − Expenses) — last 6 months</div>
    <div style="position:relative;height:170px;"><canvas id="incExpChart"></canvas></div>
  </div>

  <div class="chart-card">
    <div class="chart-title" style="margin-bottom:0;">Expenses — <span id="spendMonthLabel"></span></div>
    <div class="tabs-header">
      <button class="tab-btn active" onclick="switchTab('budget')" id="tab-b">Budget</button>
      <button class="tab-btn" onclick="switchTab('dist')" id="tab-d">Distribution</button>
    </div>
    <div class="tab-pane active" id="pane-b"><div id="budgetList"></div></div>
    <div class="tab-pane" id="pane-d">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start;">
        <div style="position:relative;height:240px;"><canvas id="donut"></canvas></div>
        <div id="distList" style="padding-top:4px;"></div>
      </div>
    </div>
  </div>

  <div class="chart-card">
    <div class="chart-title">Savings Rate — target 30%</div>
    <div class="gauge-wrap">
      <div class="gauge-svg-wrap">
        <svg viewBox="0 0 200 110" xmlns="http://www.w3.org/2000/svg">
          <path d="M20,100 A80,80 0 0,1 180,100" fill="none" stroke="#142030" stroke-width="16" stroke-linecap="round"/>
          <path id="gaugeFill" d="M20,100 A80,80 0 0,1 180,100" fill="none" stroke="#1D9E75" stroke-width="16" stroke-linecap="round" stroke-dasharray="251.2" stroke-dashoffset="251.2" style="transition:stroke-dashoffset 0.6s,stroke 0.4s;"/>
          <line x1="100" y1="20" x2="100" y2="32" stroke="#BA7517" stroke-width="2" stroke-dasharray="3,2"/>
          <text x="100" y="16" text-anchor="middle" fill="#BA7517" font-size="8" font-family="sans-serif">30%</text>
        </svg>
        <div class="gauge-center-text">
          <div class="gauge-big" id="gauge-n"></div>
          <div class="gauge-small" id="gauge-eur"></div>
        </div>
      </div>
      <div class="gauge-labels-row"><span>0%</span><span>15%</span><span>30%</span><span>45%</span><span>60%+</span></div>
      <div class="gauge-goal-label">Target: 30% monthly savings rate</div>
    </div>
  </div>

</div>

<div class="modal-overlay" id="txModalOverlay" onclick="closeTxModalOnOverlay(event)">
  <div class="modal-panel">
    <div class="modal-header">
      <div class="modal-title" id="txModalTitle"></div>
      <button class="modal-close" onclick="closeTxModal()">✕</button>
    </div>
    <div class="modal-sub" id="txModalSub"></div>
    <div class="modal-tx-list" id="txModalList"></div>
  </div>
</div>

<script>
const MONTHS = {months_json};
let currentIdx = {latest_idx};
const COLORS = ['#E91E63','#D85A30','#FF9800','#534AB7','#BA7517','#1D9E75','#0F6E56','#993C1D','#3C3489','#854F0B','#2196F3','#9C27B0','#607D8B','#795548','#009688'];

function fmt(n)  {{ return '€'+n.toLocaleString('de-DE',{{minimumFractionDigits:0,maximumFractionDigits:0}}); }}
function fmtD(n) {{ return '€'+n.toLocaleString('de-DE',{{minimumFractionDigits:2,maximumFractionDigits:2}}); }}
function pct(n)  {{ return Math.round(n)+'%'; }}
function bc(r)   {{ return r>=1?'#D85A30':r>=0.8?'#BA7517':'#1D9E75'; }}

// ── KPI trend arrow vs previous month
function trendHtml(current, prev, lowerIsBetter=false) {{
  if (prev === undefined || prev === null) return '';
  const diff = current - prev;
  if (Math.abs(diff) < 0.01) return '<span class="trend-flat">→ no change</span>';
  const better = lowerIsBetter ? diff < 0 : diff > 0;
  const cls = better ? 'trend-up' : 'trend-down';
  const arrow = diff > 0 ? '▲' : '▼';
  return `<span class="${{cls}}">${{arrow}} ${{fmt(Math.abs(diff))}} vs prev month</span>`;
}}

// ── Over-limit alert banner
function renderAlert(budgets) {{
  const overLimit = (budgets || []).filter(b => !b.p && b.l > 1 && b.s > b.l);
  const banner = document.getElementById('alertBanner');
  if (overLimit.length === 0) {{
    banner.classList.remove('visible');
    return;
  }}
  const names = overLimit.map(b => b.cat).join(', ');
  banner.textContent = `⚠ ${{overLimit.length}} ${{overLimit.length===1?'category':'categories'}} over budget this month: ${{names}}`;
  banner.classList.add('visible');
}}

// ── Budget tab
function sortB(arr){{
  const over   =arr.filter(b=>!b.p&&b.l>1&&b.s>b.l).sort((a,b)=>(b.s/b.l)-(a.s/a.l));
  const atlim  =arr.filter(b=>!b.p&&b.l>1&&b.s===b.l);
  const under  =arr.filter(b=>!b.p&&b.l>1&&b.s>0&&b.s<b.l).sort((a,b)=>(b.s/b.l)-(a.s/a.l));
  const ph     =arr.filter(b=>b.p&&b.s>0);
  const nospend=arr.filter(b=>b.s===0);
  return{{over,atlim,under,ph,nospend}};
}}
function spendRow(b,note=''){{
  const r=b.s/b.l,u=Math.min(r*100,100),c=bc(r);
  const over=b.s>b.l,diff=Math.abs(b.s-b.l);
  const dl=over?`<span style="color:#D85A30;font-size:11px;">+${{fmtD(diff)}} over limit</span>`
               :`<span style="color:#1D9E75;font-size:11px;">${{fmtD(diff)}} remaining</span>`;
  const safeCat=b.cat.replace(/'/g,"\\'");
  return`<div class="spend-item" onclick="openTxModal('${{safeCat}}')">
    <div class="spend-header"><span class="spend-cat">${{b.cat}}</span><span style="font-size:11px;font-weight:700;color:${{c}};">${{Math.round(r*100)}}% used</span></div>
    <div class="spend-amounts"><span class="spend-used" style="color:${{c}};">${{fmtD(b.s)}}</span><span class="spend-budget">limit ${{fmt(b.l)}}</span></div>
    <div class="spend-track"><div class="spend-fill" style="width:${{u}}%;background:${{c}};"></div></div>
    <div style="margin-top:3px;">${{dl}}${{note}}</div>
  </div>`;
}}
function renderBudget(budgets){{
  const{{over,atlim,under,ph,nospend}}=sortB(budgets);
  let h='';
  if(over.length||ph.length){{
    h+='<div class="section-label" style="color:#D85A30;">⚠ Over limit</div>';
    over.forEach(b=>{{h+=spendRow(b);}});
    ph.forEach(b=>{{h+=spendRow(b,'<span class="placeholder-note"> · placeholder limit</span>');}});
  }}
  if(atlim.length){{h+='<div class="section-label" style="color:#BA7517;margin-top:10px;">At limit</div>';atlim.forEach(b=>{{h+=spendRow(b);}});}}
  if(under.length){{h+='<div class="section-label" style="color:#8899AA;margin-top:10px;">Within budget</div>';under.forEach(b=>{{h+=spendRow(b);}});}}
  if(nospend.length){{
    h+='<div class="section-label" style="color:#8899AA;margin-top:10px;">No transactions</div>';
    nospend.forEach(b=>{{h+=`<div class="no-lim-row"><div class="no-lim-cat">${{b.cat}}</div><div class="no-lim-track"></div><div class="no-lim-amt">lim ${{fmt(b.l)}}</div></div>`;}});
  }}
  if(!h)h='<div class="no-month-data">No budget data for this month.</div>';
  document.getElementById('budgetList').innerHTML=h;
}}

// ── Distribution tab — now shows % of total expenses AND % of income
function renderDist(budgets, income){{
  const items=budgets.filter(b=>b.s>0).sort((a,b)=>b.s-a.s);
  if(!items.length){{
    document.getElementById('distList').innerHTML='<div class="no-month-data">No expenses recorded this month.</div>';
    if(donut){{donut.data.labels=[];donut.data.datasets[0].data=[];donut.update();}}
    return;
  }}
  const total=items.reduce((s,b)=>s+b.s,0),mx=Math.max(...items.map(b=>b.s),1);
  let h='<div style="display:flex;justify-content:flex-end;gap:8px;margin-bottom:6px;font-size:10px;color:#8899AA;padding-right:4px;"><span style="width:34px;text-align:right;">%exp</span><span style="width:38px;text-align:right;">%inc</span><span style="width:56px;text-align:right;">amount</span></div>';
  items.forEach((b,i)=>{{
    const pExp=Math.round((b.s/total)*100);
    const pInc=income>0?Math.round((b.s/income)*100):0;
    const bw=Math.round((b.s/mx)*100);
    const safeCat=b.cat.replace(/'/g,"\\'");
    h+=`<div class="dist-row" onclick="openTxModal('${{safeCat}}')">
      <div class="dist-dot" style="background:${{COLORS[i%COLORS.length]}};"></div>
      <div class="dist-cat">${{b.cat}}</div>
      <div class="dist-track"><div class="dist-fill" style="width:${{bw}}%;background:${{COLORS[i%COLORS.length]}};"></div></div>
      <div class="dist-pct">${{pExp}}%</div>
      <div class="dist-inc">${{pInc}}%</div>
      <div class="dist-amt">${{fmt(b.s)}}</div>
    </div>`;
  }});
  document.getElementById('distList').innerHTML=h;
  if(donut){{
    donut.data.labels=items.map(b=>b.cat);
    donut.data.datasets[0].data=items.map(b=>b.s);
    donut.update();
  }}
}}

// ── Semi-donut gauge
function updateGauge(sr, savingsEur){{
  const capped=Math.min(sr,60);
  const arcLen=251.2;
  const fill=arcLen*(capped/60);
  const offset=arcLen-fill;
  const path=document.getElementById('gaugeFill');
  path.style.strokeDashoffset=offset;
  path.style.stroke=sr>=30?'#1D9E75':sr>=20?'#BA7517':'#D85A30';
  document.getElementById('gauge-n').textContent=pct(sr);
  document.getElementById('gauge-n').style.color=sr>=30?'#1D9E75':sr>=20?'#BA7517':'#D85A30';
  document.getElementById('gauge-eur').textContent=fmt(savingsEur);
}}

// ── Modal
function formatTxDate(d){{
  if(!d||d.length<10)return d||'';
  const[y,mo,day]=d.split('-');return`${{day}}/${{mo}}`;
}}
window.openTxModal=function(catName){{
  const m=MONTHS[currentIdx];
  const cat=(m.budgets||[]).find(b=>b.cat===catName);
  if(!cat)return;
  document.getElementById('txModalTitle').textContent=cat.cat;
  const count=(cat.tx||[]).length;
  document.getElementById('txModalSub').textContent=`${{m.abbr}} ${{m.year}} · ${{fmtD(cat.s)}} across ${{count}} transaction${{count===1?'':'s'}}`;
  const listEl=document.getElementById('txModalList');
  if(!count){{listEl.innerHTML='<div class="modal-tx-empty">No individual transactions recorded for this category.</div>';}}
  else{{listEl.innerHTML=cat.tx.map(t=>`<div class="modal-tx-row"><span class="modal-tx-name">${{t.n}}</span><span class="modal-tx-date">${{formatTxDate(t.d)}}</span><span class="modal-tx-amt">${{fmtD(t.a)}}</span></div>`).join('');}}
  document.getElementById('txModalOverlay').classList.add('open');
  document.body.style.overflow='hidden';
}};
window.closeTxModal=function(){{document.getElementById('txModalOverlay').classList.remove('open');document.body.style.overflow='';}};
window.closeTxModalOnOverlay=function(e){{if(e.target.id==='txModalOverlay')closeTxModal();}};
document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeTxModal();}});

window.switchTab=function(tab){{
  document.getElementById('pane-b').classList.toggle('active',tab==='budget');
  document.getElementById('pane-d').classList.toggle('active',tab==='dist');
  document.getElementById('tab-b').classList.toggle('active',tab==='budget');
  document.getElementById('tab-d').classList.toggle('active',tab==='dist');
  if(tab==='dist'&&donut)donut.resize();
}};

// ── Main render function
function renderMonth(){{
  const m=MONTHS[currentIdx];
  const prev=currentIdx>0?MONTHS[currentIdx-1]:null;

  document.getElementById('monthLabel').textContent=`${{m.abbr}} ${{m.year}}`;
  document.getElementById('monthPos').textContent=`${{currentIdx+1}} / ${{MONTHS.length}}`;
  document.getElementById('spendMonthLabel').textContent=`${{m.abbr}} ${{m.year}}`;
  document.getElementById('btnPrev').disabled=currentIdx===0;
  document.getElementById('btnNext').disabled=currentIdx===MONTHS.length-1;

  // KPIs
  document.getElementById('kpi-nw').textContent=fmt(m.profits||0);
  document.getElementById('kpi-nw-trend').innerHTML=trendHtml(m.profits||0,prev?prev.profits||0:null);
  document.getElementById('kpi-inc').textContent=fmt(m.income);
  document.getElementById('kpi-inc-trend').innerHTML=trendHtml(m.income,prev?prev.income:null);
  document.getElementById('kpi-exp').textContent=fmt(m.total_expenses);
  document.getElementById('kpi-exp-pct').textContent=m.income>0?pct(m.total_expenses/m.income*100)+' of income':'—';
  document.getElementById('kpi-exp-trend').innerHTML=trendHtml(m.total_expenses,prev?prev.total_expenses:null,true);

  const sr=m.savings_rate;
  document.getElementById('kpi-sr').textContent=pct(sr);
  document.getElementById('kpi-sr-eur').textContent=fmt(m.savings||0);
  const srB=document.getElementById('kpi-sr-badge');
  if(sr>=30){{srB.textContent='✓ Target reached';srB.className='kpi-badge badge-ok';}}
  else if(sr>=20){{srB.textContent=pct(sr)+' · approaching 30%';srB.className='kpi-badge badge-warn';}}
  else{{srB.textContent=pct(sr)+' · below 30% target';srB.className='kpi-badge badge-down';}}

  updateGauge(sr,m.savings||0);
  renderAlert(m.budgets);
  renderBudget(m.budgets);
  renderDist(m.budgets,m.income);
}}

window.changeMonth=function(dir){{
  const n=currentIdx+dir;
  if(n>=0&&n<MONTHS.length){{currentIdx=n;renderMonth();}}
}};

// ── Charts (created before renderMonth to avoid ReferenceError on donut)
Chart.defaults.font.family='-apple-system,BlinkMacSystemFont,Inter,sans-serif';
Chart.defaults.color='#8899AA';

new Chart(document.getElementById('nwChart'),{{type:'line',data:{{labels:{hist_labels},datasets:[{{label:'Net Worth',data:{hist_nw},borderColor:'#534AB7',backgroundColor:'rgba(83,74,183,0.12)',borderWidth:2,pointRadius:5,pointBackgroundColor:'#534AB7',fill:true,tension:0.3}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>fmt(ctx.parsed.y)}}}}}},scales:{{x:{{grid:{{display:false}},ticks:{{font:{{size:11}}}}}},y:{{grid:{{color:'rgba(255,255,255,0.05)'}},ticks:{{font:{{size:11}},callback:v=>'€'+(v/1000).toFixed(0)+'k'}}}}}}}}}});

new Chart(document.getElementById('sumChart'),{{type:'bar',data:{{labels:{hist_labels},datasets:[{{label:'Income',data:{hist_income},backgroundColor:'rgba(29,158,117,0.8)',borderRadius:4,stack:'a'}},{{label:'Expenses',data:{hist_exp},backgroundColor:'rgba(216,90,48,0.8)',borderRadius:4,stack:'b'}},{{label:'Savings',data:{hist_savings},backgroundColor:'rgba(186,117,23,0.8)',borderRadius:4,stack:'b'}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{mode:'index',callbacks:{{label:ctx=>' '+ctx.dataset.label+': '+fmt(ctx.parsed.y)}}}}}},scales:{{x:{{grid:{{display:false}},ticks:{{font:{{size:10}}}}}},y:{{grid:{{color:'rgba(255,255,255,0.05)'}},ticks:{{font:{{size:10}},callback:v=>'€'+v}}}}}}}}}});

new Chart(document.getElementById('incExpChart'),{{type:'bar',data:{{labels:{hist_labels},datasets:[{{label:'Margin',data:{hist_margin},backgroundColor:{margin_colors},borderColor:{margin_borders},borderWidth:1.5,borderRadius:6}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>{{const v=ctx.parsed.y;return(v>=0?' Surplus: ':' Deficit: ')+fmt(Math.abs(v));}}}}}}}},scales:{{x:{{grid:{{display:false}},ticks:{{font:{{size:11}}}}}},y:{{grid:{{color:'rgba(255,255,255,0.05)'}},ticks:{{font:{{size:11}},callback:v=>'€'+(v/1000).toFixed(1)+'k'}},afterDataLimits(scale){{const abs=Math.max(Math.abs(scale.min),Math.abs(scale.max));scale.min=-abs*1.3;scale.max=abs*1.3;}}}}}}}}}});

let donut=new Chart(document.getElementById('donut'),{{type:'doughnut',data:{{labels:[],datasets:[{{data:[],backgroundColor:COLORS,borderWidth:2,borderColor:'#0f1e2d'}}]}},options:{{responsive:true,maintainAspectRatio:false,cutout:'62%',plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>' '+ctx.label+': '+fmt(ctx.parsed)}}}}}}}}}});

renderMonth();
</script>
</body>
</html>"""

    # Full per-month dataset for JS — powers KPI cards, budget tab, distribution tab
    # when the user clicks the ‹ › navigation buttons.
    months_json = json.dumps(months_data, ensure_ascii=False)

    # History arrays for the always-6-month rolling charts (independent of nav)
    chart_window = months_data[-6:]
    hist_labels   = json.dumps([h["label"] for h in chart_window])
    hist_nw       = json.dumps([h["net_worth"] for h in chart_window])
    hist_income   = json.dumps([h["income"] for h in chart_window])
    hist_exp      = json.dumps([h["total_expenses"] for h in chart_window])
    hist_savings  = json.dumps([h["savings"] for h in chart_window])
    hist_margin   = json.dumps([round(h["margin"], 2) for h in chart_window])
    margin_colors = json.dumps(["rgba(29,158,117,0.85)" if h["margin"] >= 0 else "rgba(216,90,48,0.85)" for h in chart_window])
    margin_borders = json.dumps(["#1D9E75" if h["margin"] >= 0 else "#D85A30" for h in chart_window])

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Finance Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Inter', sans-serif; background: #0D1B2A; color: #E8EDF2; padding: 20px; }}
  .dash {{ max-width: 960px; margin: 0 auto; }}
  .top-bar {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.5rem; flex-wrap: wrap; gap: 10px; }}
  .top-bar h1 {{ font-size: 18px; font-weight: 600; }}
  .notion-badge {{ font-size: 10px; background: #1a3d2e; color: #1D9E75; padding: 2px 8px; border-radius: 20px; font-weight: 700; margin-left: 8px; }}
  .updated-badge {{ font-size: 10px; color: #8899AA; margin-left: 6px; }}
  .month-nav {{ display: flex; align-items: center; gap: 10px; }}
  .month-nav button {{ background: #1a2a3a; border: 0.5px solid #2a3a4a; border-radius: 8px; padding: 4px 12px; cursor: pointer; color: #E8EDF2; font-size: 13px; }}
  .month-nav button:hover:not(:disabled) {{ background: #2a3a4a; }}
  .month-nav button:disabled {{ opacity: 0.35; cursor: not-allowed; }}
  .month-label {{ font-size: 14px; font-weight: 600; min-width: 110px; text-align: center; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 1.25rem; }}
  .kpi {{ background: #142030; border-radius: 12px; padding: 16px; border-left: 3px solid transparent; }}
  .kpi.net-worth {{ border-left-color: #534AB7; }}
  .kpi.income    {{ border-left-color: #1D9E75; }}
  .kpi.expenses  {{ border-left-color: #D85A30; }}
  .kpi.savings   {{ border-left-color: #BA7517; }}
  .kpi-label {{ font-size: 10px; color: #8899AA; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 6px; }}
  .kpi-value {{ font-size: 24px; font-weight: 700; }}
  .kpi-sub {{ font-size: 11px; color: #8899AA; margin-top: 4px; }}
  .kpi-badge {{ display: inline-block; font-size: 11px; padding: 2px 7px; border-radius: 20px; margin-top: 5px; font-weight: 500; }}
  .badge-up   {{ background: #0f2a1e; color: #1D9E75; }}
  .badge-down {{ background: #2a1010; color: #D85A30; }}
  .badge-warn {{ background: #2a1e08; color: #BA7517; }}
  .badge-ok   {{ background: #1a1830; color: #8B83FF; }}
  .charts-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }}
  .chart-card {{ background: #0f1e2d; border: 0.5px solid #1e2e3e; border-radius: 12px; padding: 16px; margin-bottom: 12px; }}
  .chart-title {{ font-size: 11px; font-weight: 600; color: #8899AA; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em; }}
  .legend-row {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 10px; font-size: 12px; color: #8899AA; }}
  .legend-dot {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; margin-right: 4px; }}
  .savings-rate-wrap {{ display: flex; flex-direction: column; align-items: center; padding: 8px 0; }}
  .gauge-num {{ font-size: 36px; font-weight: 700; margin: 8px 0 2px; }}
  .gauge-goal {{ font-size: 12px; color: #8899AA; }}
  .gauge-bar-track {{ width: 100%; max-width: 400px; height: 10px; background: #142030; border-radius: 10px; margin: 12px 0 4px; overflow: hidden; }}
  .gauge-bar-fill {{ height: 100%; border-radius: 10px; transition: width 0.4s; }}
  .gauge-labels {{ display: flex; justify-content: space-between; width: 100%; max-width: 400px; font-size: 11px; color: #8899AA; }}
  .tabs-header {{ display: flex; margin-bottom: 14px; border-bottom: 0.5px solid #1e2e3e; }}
  .tab-btn {{ padding: 7px 16px; font-size: 12px; font-weight: 600; cursor: pointer; background: none; border: none; border-bottom: 2px solid transparent; color: #8899AA; margin-bottom: -1px; }}
  .tab-btn.active {{ color: #E8EDF2; border-bottom-color: #534AB7; }}
  .tab-pane {{ display: none; }}
  .tab-pane.active {{ display: block; }}
  .section-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; margin: 10px 0 8px; font-weight: 600; }}
  .spend-item {{ margin-bottom: 12px; cursor: pointer; padding: 8px; margin: -8px -8px 4px -8px; border-radius: 8px; transition: background 0.15s; }}
  .spend-item:hover {{ background: #142030; }}
  .spend-header {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }}
  .spend-cat {{ font-size: 12px; font-weight: 600; }}
  .spend-amounts {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px; }}
  .spend-used {{ font-size: 13px; font-weight: 700; }}
  .spend-budget {{ font-size: 11px; color: #8899AA; }}
  .spend-track {{ width: 100%; height: 7px; background: #142030; border-radius: 4px; overflow: hidden; }}
  .spend-fill {{ height: 100%; border-radius: 4px; }}
  .no-lim-row {{ margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }}
  .no-lim-cat {{ font-size: 12px; color: #8899AA; width: 150px; flex-shrink: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .no-lim-track {{ flex: 1; height: 6px; background: #142030; border-radius: 4px; }}
  .no-lim-amt {{ font-size: 11px; color: #8899AA; width: 60px; text-align: right; flex-shrink: 0; }}
  .dist-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 9px; cursor: pointer; padding: 6px 8px; margin-left: -8px; margin-right: -8px; border-radius: 8px; transition: background 0.15s; }}
  .dist-row:hover {{ background: #142030; }}
  .dist-dot {{ width: 10px; height: 10px; border-radius: 2px; flex-shrink: 0; }}
  .dist-cat {{ font-size: 12px; width: 130px; flex-shrink: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .dist-track {{ flex: 1; height: 7px; background: #142030; border-radius: 4px; overflow: hidden; }}
  .dist-fill {{ height: 100%; border-radius: 4px; }}
  .dist-pct {{ font-size: 11px; color: #8899AA; width: 36px; text-align: right; flex-shrink: 0; }}
  .dist-amt {{ font-size: 11px; color: #8899AA; width: 60px; text-align: right; flex-shrink: 0; }}
  .fx-note {{ font-size: 10px; color: #8899AA; font-style: italic; margin-top: 6px; text-align: center; }}
  .placeholder-note {{ font-size: 10px; color: #BA7517; }}
  .no-month-data {{ font-size: 12px; color: #8899AA; text-align: center; padding: 30px 0; font-style: italic; }}

  /* Transaction detail modal */
  .modal-overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.55); z-index: 100; align-items: flex-start; justify-content: center; padding: 8vh 16px; overflow-y: auto; }}
  .modal-overlay.open {{ display: flex; }}
  .modal-panel {{ background: #0f1e2d; border: 0.5px solid #2a3a4a; border-radius: 14px; padding: 20px; width: 100%; max-width: 480px; max-height: 76vh; display: flex; flex-direction: column; box-shadow: 0 20px 60px rgba(0,0,0,0.5); }}
  .modal-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }}
  .modal-title {{ font-size: 15px; font-weight: 700; }}
  .modal-close {{ background: #1a2a3a; border: none; color: #8899AA; width: 26px; height: 26px; border-radius: 7px; cursor: pointer; font-size: 13px; line-height: 1; }}
  .modal-close:hover {{ background: #2a3a4a; color: #E8EDF2; }}
  .modal-sub {{ font-size: 11px; color: #8899AA; margin-bottom: 14px; }}
  .modal-tx-list {{ overflow-y: auto; }}
  .modal-tx-row {{ display: flex; justify-content: space-between; align-items: baseline; padding: 9px 0; border-bottom: 0.5px solid #1e2e3e; gap: 12px; }}
  .modal-tx-row:last-child {{ border-bottom: none; }}
  .modal-tx-name {{ font-size: 12px; color: #E8EDF2; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .modal-tx-date {{ font-size: 10px; color: #8899AA; flex-shrink: 0; }}
  .modal-tx-amt {{ font-size: 12px; font-weight: 700; flex-shrink: 0; min-width: 64px; text-align: right; }}
  .modal-tx-empty {{ font-size: 12px; color: #8899AA; text-align: center; padding: 24px 0; font-style: italic; }}
  @media(max-width:600px){{.kpi-grid{{grid-template-columns:1fr 1fr;}}.charts-row{{grid-template-columns:1fr;}}}}
</style>
</head>
<body>
<div class="dash">
  <div class="top-bar">
    <h1>💰 Finance Dashboard<span class="notion-badge">● NOTION LIVE</span><span class="updated-badge">{now_str}</span></h1>
    <div class="month-nav">
      <button id="btnPrev" onclick="changeMonth(-1)">‹</button>
      <span class="month-label" id="monthLabel"></span>
      <button id="btnNext" onclick="changeMonth(1)">›</button>
    </div>
  </div>

  <div class="kpi-grid">
    <div class="kpi net-worth"><div class="kpi-label">Profits</div><div class="kpi-value" id="kpi-nw"></div><div class="kpi-sub">Market revaluation</div></div>
    <div class="kpi income"><div class="kpi-label">Income</div><div class="kpi-value" id="kpi-inc"></div><div class="kpi-badge badge-up">Salary + Other Income</div></div>
    <div class="kpi expenses"><div class="kpi-label">Expenses</div><div class="kpi-value" id="kpi-exp"></div><div class="kpi-badge badge-down" id="kpi-exp-pct"></div></div>
    <div class="kpi savings"><div class="kpi-label">Savings Rate</div><div class="kpi-value" id="kpi-sr"></div><div class="kpi-sub" id="kpi-sr-eur"></div><div class="kpi-badge" id="kpi-sr-badge"></div></div>
  </div>

  <div class="charts-row">
    <div class="chart-card" style="margin-bottom:0;">
      <div class="chart-title">Net Worth — últimos 6 meses</div>
      <div style="position:relative;height:180px;"><canvas id="nwChart"></canvas></div>
    </div>
    <div class="chart-card" style="margin-bottom:0;">
      <div class="chart-title">Monthly Summary</div>
      <div class="legend-row">
        <span><span class="legend-dot" style="background:#1D9E75"></span>Income</span>
        <span><span class="legend-dot" style="background:#D85A30"></span>Expenses</span>
        <span><span class="legend-dot" style="background:#BA7517"></span>Savings</span>
      </div>
      <div style="position:relative;height:155px;"><canvas id="sumChart"></canvas></div>
    </div>
  </div>

  <div class="chart-card">
    <div class="chart-title">Margen mensual (Income − Expenses) — últimos 6 meses</div>
    <div style="position:relative;height:180px;"><canvas id="incExpChart"></canvas></div>
  </div>

  <div class="chart-card">
    <div class="chart-title" style="margin-bottom:0;">Gastos — <span id="spendMonthLabel"></span></div>
    <div class="tabs-header">
      <button class="tab-btn active" onclick="switchTab('budget')" id="tab-b">Presupuesto</button>
      <button class="tab-btn" onclick="switchTab('dist')" id="tab-d">Distribución</button>
    </div>
    <div class="tab-pane active" id="pane-b"><div id="budgetList"></div></div>
    <div class="tab-pane" id="pane-d">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start;">
        <div style="position:relative;height:240px;"><canvas id="donut"></canvas></div>
        <div id="distList" style="padding-top:4px;"></div>
      </div>
    </div>
  </div>

  <div class="chart-card">
    <div class="chart-title">Savings Rate — objetivo 30%</div>
    <div class="savings-rate-wrap">
      <div class="gauge-num" id="gauge-n"></div>
      <div class="gauge-goal">Objetivo: 30% mensual</div>
      <div class="gauge-bar-track"><div class="gauge-bar-fill" id="gauge-f"></div></div>
      <div class="gauge-labels"><span>0%</span><span>15%</span><span>30%</span><span>45%</span><span>60%</span></div>
    </div>
  </div>
</div>

<div class="modal-overlay" id="txModalOverlay" onclick="closeTxModalOnOverlay(event)">
  <div class="modal-panel">
    <div class="modal-header">
      <div class="modal-title" id="txModalTitle"></div>
      <button class="modal-close" onclick="closeTxModal()">✕</button>
    </div>
    <div class="modal-sub" id="txModalSub"></div>
    <div class="modal-tx-list" id="txModalList"></div>
  </div>
</div>

<script>
// ══ FULL MONTH-BY-MONTH DATASET — powers real navigation ═══════════════════
const MONTHS = {months_json};
let currentIdx = {latest_idx};

const COLORS = ['#E91E63','#D85A30','#FF9800','#534AB7','#BA7517','#1D9E75','#0F6E56','#993C1D','#3C3489','#854F0B','#2196F3','#9C27B0','#607D8B','#795548','#009688'];

function fmt(n)  {{ return '€'+n.toLocaleString('de-DE',{{minimumFractionDigits:0,maximumFractionDigits:0}}); }}
function fmtD(n) {{ return '€'+n.toLocaleString('de-DE',{{minimumFractionDigits:2,maximumFractionDigits:2}}); }}
function pct(n)  {{ return Math.round(n)+'%'; }}
function bc(r)   {{ return r>=1?'#D85A30':r>=0.8?'#BA7517':'#1D9E75'; }}

function sortB(arr){{
  const over   =arr.filter(b=>!b.p&&b.l>1&&b.s>b.l).sort((a,b)=>(b.s/b.l)-(a.s/a.l));
  const atlim  =arr.filter(b=>!b.p&&b.l>1&&b.s===b.l);
  const under  =arr.filter(b=>!b.p&&b.l>1&&b.s>0&&b.s<b.l).sort((a,b)=>(b.s/b.l)-(a.s/a.l));
  const ph     =arr.filter(b=>b.p&&b.s>0);
  const nospend=arr.filter(b=>b.s===0);
  return{{over,atlim,under,ph,nospend}};
}}
function spendRow(b,note=''){{
  const r=b.s/b.l,u=Math.min(r*100,100),c=bc(r);
  const over=b.s>b.l,diff=Math.abs(b.s-b.l);
  const dl=over?`<span style="color:#D85A30;font-size:11px;">+${{fmtD(diff)}} sobre límite</span>`:`<span style="color:#1D9E75;font-size:11px;">${{fmtD(diff)}} disponible</span>`;
  const safeCat = b.cat.replace(/'/g, "\\'");
  return`<div class="spend-item" onclick="openTxModal('${{safeCat}}')">
    <div class="spend-header"><span class="spend-cat">${{b.cat}}</span><span style="font-size:11px;font-weight:700;color:${{c}};">${{Math.round(r*100)}}% usado</span></div>
    <div class="spend-amounts"><span class="spend-used" style="color:${{c}};">${{fmtD(b.s)}}</span><span class="spend-budget">límite ${{fmt(b.l)}}</span></div>
    <div class="spend-track"><div class="spend-fill" style="width:${{u}}%;background:${{c}};"></div></div>
    <div style="margin-top:3px;">${{dl}}${{note}}</div>
  </div>`;
}}
function renderBudget(budgets){{
  const{{over,atlim,under,ph,nospend}}=sortB(budgets);
  let h='';
  if(over.length||ph.length){{h+='<div class="section-label" style="color:#D85A30;">⚠ Sobre el límite</div>';over.forEach(b=>{{h+=spendRow(b);}});ph.forEach(b=>{{h+=spendRow(b,'<span class="placeholder-note"> · límite placeholder</span>');}});}}
  if(atlim.length){{h+='<div class="section-label" style="color:#BA7517;margin-top:10px;">En el límite exacto</div>';atlim.forEach(b=>{{h+=spendRow(b);}});}}
  if(under.length){{h+='<div class="section-label" style="color:#8899AA;margin-top:10px;">Dentro del presupuesto</div>';under.forEach(b=>{{h+=spendRow(b);}});}}
  if(nospend.length){{h+='<div class="section-label" style="color:#8899AA;margin-top:10px;">Sin transacciones</div>';nospend.forEach(b=>{{h+=`<div class="no-lim-row"><div class="no-lim-cat">${{b.cat}}</div><div class="no-lim-track"></div><div class="no-lim-amt">lím. ${{fmt(b.l)}}</div></div>`;}});}}
  if(!h) h = '<div class="no-month-data">Sin datos de presupuesto para este mes.</div>';
  document.getElementById('budgetList').innerHTML=h;
}}
function renderDist(budgets){{
  const items=budgets.filter(b=>b.s>0).sort((a,b)=>b.s-a.s);
  if(!items.length){{
    document.getElementById('distList').innerHTML='<div class="no-month-data">Sin gastos registrados este mes.</div>';
    if(donut){{ donut.data.labels=[]; donut.data.datasets[0].data=[]; donut.update(); }}
    return;
  }}
  const total=items.reduce((s,b)=>s+b.s,0),mx=Math.max(...items.map(b=>b.s),1);
  let h='';
  items.forEach((b,i)=>{{const p=Math.round((b.s/total)*100),bw=Math.round((b.s/mx)*100);const safeCat=b.cat.replace(/'/g, "\\'");h+=`<div class="dist-row" onclick="openTxModal('${{safeCat}}')"><div class="dist-dot" style="background:${{COLORS[i%COLORS.length]}};"></div><div class="dist-cat">${{b.cat}}</div><div class="dist-track"><div class="dist-fill" style="width:${{bw}}%;background:${{COLORS[i%COLORS.length]}};"></div></div><div class="dist-pct">${{p}}%</div><div class="dist-amt">${{fmt(b.s)}}</div></div>`;}});
  document.getElementById('distList').innerHTML=h;
  if(donut){{
    donut.data.labels=items.map(b=>b.cat);
    donut.data.datasets[0].data=items.map(b=>b.s);
    donut.update();
  }}
}}

// ── Transaction detail modal ─────────────────────────────────────────────
// Looks up the clicked category within the CURRENTLY DISPLAYED month
// (MONTHS[currentIdx]) and lists every underlying transaction.
function formatTxDate(isoDate) {{
  // isoDate is "YYYY-MM-DD" — render as "DD/MM"
  if (!isoDate || isoDate.length < 10) return isoDate || '';
  const [y, m, d] = isoDate.split('-');
  return `${{d}}/${{m}}`;
}}

window.openTxModal = function(catName) {{
  const m = MONTHS[currentIdx];
  const cat = (m.budgets || []).find(b => b.cat === catName);
  if (!cat) return;

  document.getElementById('txModalTitle').textContent = cat.cat;
  const count = (cat.tx || []).length;
  document.getElementById('txModalSub').textContent =
    `${{m.label}} ${{m.year}} · ${{fmtD(cat.s)}} en ${{count}} transacci${{count===1?'ón':'ones'}}`;

  const listEl = document.getElementById('txModalList');
  if (!count) {{
    listEl.innerHTML = '<div class="modal-tx-empty">Sin transacciones individuales registradas para esta categoría.</div>';
  }} else {{
    listEl.innerHTML = cat.tx.map(t => `
      <div class="modal-tx-row">
        <span class="modal-tx-name">${{t.n}}</span>
        <span class="modal-tx-date">${{formatTxDate(t.d)}}</span>
        <span class="modal-tx-amt">${{fmtD(t.a)}}</span>
      </div>
    `).join('');
  }}

  document.getElementById('txModalOverlay').classList.add('open');
  document.body.style.overflow = 'hidden';
}};

window.closeTxModal = function() {{
  document.getElementById('txModalOverlay').classList.remove('open');
  document.body.style.overflow = '';
}};

window.closeTxModalOnOverlay = function(evt) {{
  if (evt.target.id === 'txModalOverlay') closeTxModal();
}};

document.addEventListener('keydown', function(evt) {{
  if (evt.key === 'Escape') closeTxModal();
}});

window.switchTab=function(tab){{
  document.getElementById('pane-b').classList.toggle('active',tab==='budget');
  document.getElementById('pane-d').classList.toggle('active',tab==='dist');
  document.getElementById('tab-b').classList.toggle('active',tab==='budget');
  document.getElementById('tab-d').classList.toggle('active',tab==='dist');
  if(tab==='dist'&&donut)donut.resize();
}};

// ── Renders the currently-selected month's KPIs, budget tab, and distribution tab
function renderMonth(){{
  const m = MONTHS[currentIdx];

  document.getElementById('monthLabel').textContent = `${{m.label}} ${{m.year}}`;
  document.getElementById('spendMonthLabel').textContent = `${{m.label.toLowerCase()}} ${{m.year}}`;
  document.getElementById('btnPrev').disabled = currentIdx === 0;
  document.getElementById('btnNext').disabled = currentIdx === MONTHS.length - 1;

  document.getElementById('kpi-nw').textContent = fmt(m.profits || 0);
  document.getElementById('kpi-inc').textContent = fmt(m.income);
  document.getElementById('kpi-exp').textContent = fmt(m.total_expenses);
  document.getElementById('kpi-exp-pct').textContent = m.income > 0 ? (pct(m.total_expenses/m.income*100)+' del income') : '—';

  const sr = m.savings_rate;
  document.getElementById('kpi-sr').textContent = pct(sr);
  document.getElementById('kpi-sr-eur').textContent = fmt(m.savings || 0);
  const srB=document.getElementById('kpi-sr-badge');
  if(sr>=30){{srB.textContent='✓ Objetivo alcanzado';srB.className='kpi-badge badge-ok';}}
  else if(sr>=20){{srB.textContent=pct(sr)+' · cerca del 30%';srB.className='kpi-badge badge-warn';}}
  else{{srB.textContent=pct(sr)+' · por debajo del 30%';srB.className='kpi-badge badge-down';}}

  document.getElementById('gauge-n').textContent=pct(sr);
  document.getElementById('gauge-f').style.width=Math.min((sr/60)*100,100)+'%';
  document.getElementById('gauge-f').style.background=sr>=30?'#1D9E75':sr>=20?'#BA7517':'#D85A30';

  renderBudget(m.budgets);
  renderDist(m.budgets);
}}

// ── Rolling 6-month charts (independent of the month navigator above) ───────
// IMPORTANT: these must be created BEFORE the first renderMonth() call,
// because renderMonth() -> renderDist() references the `donut` chart object.
Chart.defaults.font.family='-apple-system,BlinkMacSystemFont,Inter,sans-serif';
Chart.defaults.color='#8899AA';

new Chart(document.getElementById('nwChart'),{{type:'line',data:{{labels:{hist_labels},datasets:[{{label:'Net Worth',data:{hist_nw},borderColor:'#534AB7',backgroundColor:'rgba(83,74,183,0.12)',borderWidth:2,pointRadius:5,pointBackgroundColor:'#534AB7',fill:true,tension:0.3}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>fmt(ctx.parsed.y)}}}}}},scales:{{x:{{grid:{{display:false}},ticks:{{font:{{size:11}}}}}},y:{{grid:{{color:'rgba(255,255,255,0.05)'}},ticks:{{font:{{size:11}},callback:v=>'€'+(v/1000).toFixed(0)+'k'}}}}}}}}}});

new Chart(document.getElementById('sumChart'),{{type:'bar',data:{{labels:{hist_labels},datasets:[{{label:'Income',data:{hist_income},backgroundColor:'#1D9E75',borderRadius:4}},{{label:'Expenses',data:{hist_exp},backgroundColor:'#D85A30',borderRadius:4}},{{label:'Savings',data:{hist_savings},backgroundColor:'#BA7517',borderRadius:4}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>' '+ctx.dataset.label+': '+fmt(ctx.parsed.y)}}}}}},scales:{{x:{{grid:{{display:false}},ticks:{{font:{{size:10}}}}}},y:{{grid:{{color:'rgba(255,255,255,0.05)'}},ticks:{{font:{{size:10}},callback:v=>'€'+v}}}}}}}}}});

new Chart(document.getElementById('incExpChart'),{{type:'bar',data:{{labels:{hist_labels},datasets:[{{label:'Margen',data:{hist_margin},backgroundColor:{margin_colors},borderColor:{margin_borders},borderWidth:1.5,borderRadius:6}}]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>{{const v=ctx.parsed.y;return(v>=0?' Superávit: ':' Déficit: ')+fmt(Math.abs(v));}}}}}}}},scales:{{x:{{grid:{{display:false}},ticks:{{font:{{size:11}}}}}},y:{{grid:{{color:'rgba(255,255,255,0.05)'}},ticks:{{font:{{size:11}},callback:v=>'€'+(v/1000).toFixed(1)+'k'}},afterDataLimits(scale){{const abs=Math.max(Math.abs(scale.min),Math.abs(scale.max));scale.min=-abs*1.3;scale.max=abs*1.3;}}}}}}}}}});

let donut = new Chart(document.getElementById('donut'),{{type:'doughnut',data:{{labels:[],datasets:[{{data:[],backgroundColor:COLORS,borderWidth:2,borderColor:'#0f1e2d'}}]}},options:{{responsive:true,maintainAspectRatio:false,cutout:'62%',plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:ctx=>' '+ctx.label+': '+fmt(ctx.parsed)}}}}}}}}}});

// NOW it's safe to render the initial month — donut already exists.
renderMonth();

window.changeMonth = function(dir) {{
  const newIdx = currentIdx + dir;
  if (newIdx >= 0 && newIdx < MONTHS.length) {{
    currentIdx = newIdx;
    renderMonth();
  }}
}};
</script>
</body>
</html>"""

# ── History management ─────────────────────────────────────────────────────────
OUTPUT_DIR   = Path("output")
HISTORY_FILE = OUTPUT_DIR / "history.json"

def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []

def save_history(history: list[dict]):
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

def update_history(history: list[dict], month_label: str, fin: dict, net_worth: float,
                    label: str = "", abbr: str = "", year: int = 0,
                    profits: float = 0.0) -> list[dict]:
    entry = {
        "label":         label or month_label,
        "abbr":          abbr,
        "year":          year,
        "income":        round(fin["income"], 2),
        "expenses":      round(fin["total_expenses"], 2),
        "total_expenses": round(fin["total_expenses"], 2),
        "savings":       round(fin["savings"], 2),
        "savings_rate":  round(fin["savings_rate"], 1),
        "margin":        round(fin["margin"], 2),
        "net_worth":     net_worth,
        "profits":       round(profits, 2),
        "budgets":       build_budget_js(fin["expenses"]),
    }
    period_key = f"{abbr} {year}" if abbr and year else month_label
    # Replace if same period already exists, else append
    updated = [e for e in history if f"{e.get('abbr','')} {e.get('year','')}" != period_key]
    updated.append(entry)
    return updated  # keep FULL history — truncation to last 6 happens only when rendering charts

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    today = date.today()

    print(f"\n🔄 Finance Dashboard Generator")
    print(f"   Mode:  ALL HISTORY (reads every month found in Notion Budgets DB)")
    print(f"   Date:  {today.isoformat()}\n")

    # 0. Check Notion connection before doing anything
    if not check_notion_connection():
        print("\n💥 Aborting — fix the connection issue above and try again.")
        raise SystemExit(1)

    # 1. Discover every (month, year) combination present in the Budgets DB
    print("🔍 Discovering all periods in Notion...")
    periods = discover_all_periods()
    if not periods:
        print("   ❌ No budgets found in the database at all. Nothing to generate.")
        raise SystemExit(1)
    print(f"   Found {len(periods)} period(s): {[f'{m} {y}' for m, y in periods]}\n")

    # 1.5 Calculate the FIXED base Net Worth from Initial Balances ONCE.
    #     Each month's Net Worth = base + cumulative(Income - Expenses) up to
    #     and including that month — i.e. it grows/shrinks purely from your
    #     tracked monthly cash flow, exactly as if every euro in/out were
    #     reflected directly in the accounts.
    base_net_worth = calculate_base_net_worth()

    # 2. Read + classify every period, building full history with a RUNNING
    #    cumulative Net Worth (oldest month first, since periods is already
    #    sorted chronologically).
    history: list[dict] = []
    last_label, last_abbr_final, last_year_final = "", "", 0
    last_abbr, last_year = periods[-1]
    running_net_worth = base_net_worth

    for abbr, year in periods:
        month_num = next(num for num, a in MONTH_ABBR.items() if a == abbr)
        label = MONTH_NAMES_ES[month_num]

        print(f"📥 Reading {label} {year} ({abbr})...")
        budgets = query_budgets(abbr, year)

        # Query Savings (Invest-type transactions) directly from Transactions DB
        print(f"💰 Querying Savings (Invest transactions) for {label} {year}...")
        savings_total = query_savings(abbr, year)

        fin = classify(budgets, savings_override=savings_total)
        print(f"   Income:   €{fin['income']:.2f}")
        print(f"   Savings:  €{fin['savings']:.2f}  (SR: {fin['savings_rate']:.1f}%)")
        print(f"   Expenses: €{fin['total_expenses']:.2f}")
        print(f"   Margin:   €{fin['margin']:.2f}")

        # Query Profits transactions directly from Transactions DB (not linked to Budgets)
        print(f"📈 Querying Profits transactions for {label} {year}...")
        profits = query_profits(abbr, year)

        running_net_worth += fin["margin"]
        net_worth = round(running_net_worth, 2)
        print(f"   Net Worth (cumulative): €{net_worth:,.2f}\n")

        history = update_history(history, f"{abbr} {year}", fin, net_worth,
                                 label=label, abbr=abbr, year=year, profits=profits)

        if (abbr, year) == (last_abbr, last_year):
            last_label, last_abbr_final, last_year_final = label, abbr, year

    # 3. No need to preserve old net_worth values anymore — the cumulative
    #    model recomputes every month's Net Worth fresh from the fixed base
    #    plus that month's real Income/Expenses, every time the script runs.

    # Sort chronologically (oldest first) so navigation and chart windows are correct
    abbr_order = {v: k for k, v in MONTH_ABBR.items()}
    history.sort(key=lambda h: (h.get("year", 0), abbr_order.get(h.get("abbr", ""), 0)))

    save_history(history)
    print(f"📈 Full history saved: {len(history)} month(s) total")
    print(f"   {[h['label']+' '+str(h['year']) for h in history]}\n")

    # 4. Generate HTML with full month-by-month dataset for real navigation.
    #    Defaults to showing the latest (most recent) month.
    print(f"🎨 Generating dashboard HTML — default view: {last_label} {last_year_final}...")
    latest_idx = len(history) - 1  # history is chronological, latest is last

    html = generate_html(history, latest_idx)

    output_path = OUTPUT_DIR / "dashboard.html"
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"   ✅ Saved to {output_path}  ({len(html):,} bytes)")

if __name__ == "__main__":
    main()