# seed_jira.py
"""
Seeds your Jira project with ~25 realistic issues for testing the
Discovery Agent: epics, stories with/without acceptance criteria,
bugs, subtasks, "blocks" links, decision comments, near-duplicates,
one conflict (AC vs comment), vague items and orphans.

Every seeded issue gets the label `adlc-seed`, so it is safe to re-run
(already-created summaries are skipped) and easy to clean up.

Usage:
    python seed_jira.py              # create issues
    python seed_jira.py --cleanup    # delete everything labelled adlc-seed
"""

import argparse
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ["JIRA_URL"].rstrip("/")
AUTH = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
PROJECT = os.environ.get("JIRA_PROJECT_KEY", "KAN")
LABEL = "adlc-seed"

S = requests.Session()
S.auth = AUTH
S.headers.update({"Accept": "application/json", "Content-Type": "application/json"})


# ----------------------------------------------------------------------
# Seed data. `ref` is a local id used for parents/links/comments.
# Description mini-markup:  "## " heading, "- " bullet, blank line = paragraph
# ----------------------------------------------------------------------
EPICS = [
    {"ref": "E_AUTH", "summary": "User Authentication",
     "desc": "All sign-in, sign-up and account recovery flows for the web app."},
    {"ref": "E_REPORT", "summary": "Reporting & Analytics",
     "desc": "Reports, dashboards and exports for account admins."},
    {"ref": "E_PAY", "summary": "Payments",
     "desc": "Checkout payments, saved payment methods and refunds."},
    {"ref": "E_NOTIFY", "summary": "Notifications",
     "desc": "Email, SMS and in-app notifications."},
]

ISSUES = [
    # ---- Authentication ----
    {"ref": "AUTH_LOGIN", "type": "story", "epic": "E_AUTH", "priority": "High",
     "summary": "Email and password login",
     "desc": "As a user, I want to log in with my email and password so that I can access my account.\n\n"
             "## Acceptance Criteria\n- Valid credentials redirect to dashboard\n"
             "- Invalid credentials show a generic error\n- Account locks after 5 failed attempts"},
    {"ref": "AUTH_SSO", "type": "story", "epic": "E_AUTH", "priority": "Medium",
     "summary": "Google SSO login",
     "desc": "As a user, I want to sign in with my Google account so I don't need another password."},
    {"ref": "AUTH_SSO_DUP", "type": "story", "epic": "E_AUTH",
     "summary": "Allow users to sign in using Gmail",
     "desc": "Customers keep asking to use their Gmail account to log in instead of creating a new password."},
    {"ref": "AUTH_RESET", "type": "story", "epic": "E_AUTH", "priority": "High",
     "summary": "Password reset via email",
     "desc": "As a user, I want to reset my password via an emailed link.\n\n"
             "## Acceptance Criteria\n- Reset link is emailed within 1 minute\n"
             "- Link expires after 24 hours\n- Old password stops working after reset"},
    {"ref": "AUTH_SAFARI", "type": "bug", "epic": "E_AUTH", "priority": "Highest",
     "summary": "Login button unresponsive on Safari",
     "desc": "On Safari 17 (macOS), clicking Login does nothing. Console shows a CORS error on /api/session. "
             "Chrome and Firefox work fine."},

    # ---- Reporting ----
    {"ref": "REP_PDF", "type": "story", "epic": "E_REPORT", "priority": "Medium",
     "summary": "Export monthly report as PDF",
     "desc": "As an admin, I want to download the monthly report as a PDF to share with leadership.\n\n"
             "## Acceptance Criteria\n- PDF matches on-screen layout\n- Includes company logo\n"
             "- Respects active filters"},
    {"ref": "REP_WIDGETS", "type": "story", "epic": "E_REPORT",
     "summary": "Customizable dashboard widgets",
     "desc": "Admins want to add, remove and rearrange dashboard widgets."},
    {"ref": "REP_SLOW", "type": "bug", "epic": "E_REPORT", "priority": "High",
     "summary": "Dashboard takes 30+ seconds for large enterprise customers",
     "desc": "Enterprise tenants with large datasets see the dashboard spinner for 30-60 seconds, "
             "sometimes it times out entirely."},
    {"ref": "REP_SCHEDULE", "type": "story", "epic": "E_REPORT",
     "summary": "Scheduled weekly report emails",
     "desc": "As an admin, I want the weekly report emailed to me every Monday at 9am.\n\n"
             "## Acceptance Criteria\n- Admin can choose day and time\n- Email contains PDF attachment"},

    # ---- Payments ----
    {"ref": "PAY_GATEWAY", "type": "task", "epic": "E_PAY", "priority": "Highest",
     "summary": "Integrate Razorpay payment gateway SDK",
     "desc": "Set up Razorpay SDK, sandbox keys, webhook endpoint and signature verification."},
    {"ref": "PAY_UPI", "type": "story", "epic": "E_PAY", "priority": "High",
     "summary": "Pay with UPI at checkout",
     "desc": "As a customer in India, I want to pay using UPI so checkout is faster.\n\n"
             "## Acceptance Criteria\n- QR code and UPI ID options shown\n"
             "- Payment status updates within 10 seconds\n- Order is cancelled if unpaid after 5 minutes"},
    {"ref": "PAY_CARDS", "type": "story", "epic": "E_PAY", "priority": "Medium",
     "summary": "Save cards for faster checkout",
     "desc": "Returning customers want to save their card so they don't re-enter it every time."},
    {"ref": "PAY_REFUND", "type": "story", "epic": "E_PAY",
     "summary": "Refund processing", "desc": ""},

    # ---- Notifications ----
    {"ref": "NOT_EMAIL", "type": "task", "epic": "E_NOTIFY", "priority": "High",
     "summary": "Set up transactional email service (SendGrid)",
     "desc": "Configure SendGrid account, domain verification (SPF/DKIM) and a reusable email template service."},
    {"ref": "NOT_INAPP", "type": "story", "epic": "E_NOTIFY",
     "summary": "In-app notification centre",
     "desc": "Bell icon with unread count; list of recent notifications; mark as read."},
    {"ref": "NOT_SMS", "type": "story", "epic": "E_NOTIFY",
     "summary": "SMS OTP",
     "desc": "We need OTP over SMS."},

    # ---- Orphans / vague ----
    {"ref": "ORPH_PG", "type": "task", "priority": "Low",
     "summary": "Upgrade PostgreSQL from 13 to 16",
     "desc": "Postgres 13 reaches end of life soon. Upgrade staging first, then production."},
    {"ref": "ORPH_PERF", "type": "story",
     "summary": "Improve performance",
     "desc": "The app feels slow."},
    {"ref": "ORPH_TYPO", "type": "bug", "priority": "Lowest",
     "summary": "Typo on pricing page: 'Anual' should be 'Annual'",
     "desc": "Pricing page toggle label is misspelled."},
]

SUBTASKS = [
    {"ref": "UPI_QR", "parent": "PAY_UPI", "summary": "Generate dynamic UPI QR code",
     "desc": "Generate a per-order QR code using the gateway API."},
    {"ref": "UPI_TIMEOUT", "parent": "PAY_UPI", "summary": "Handle UPI payment timeout",
     "desc": "Cancel order and release inventory if payment isn't confirmed in 5 minutes."},
]

# (blocker, blocked)
BLOCKS = [
    ("PAY_GATEWAY", "PAY_UPI"),
    ("PAY_GATEWAY", "PAY_CARDS"),
    ("PAY_GATEWAY", "PAY_REFUND"),
    ("NOT_EMAIL", "AUTH_RESET"),
    ("NOT_EMAIL", "REP_SCHEDULE"),
    ("REP_PDF", "REP_SCHEDULE"),
]

COMMENTS = {
    "AUTH_SSO": ["Client confirmed in the 2 Oct call: only Google for MVP. Microsoft SSO can come in phase 2."],
    "AUTH_RESET": ["Security team review: reset links must expire after 15 minutes, not 24 hours."],  # conflict
    "PAY_CARDS": ["Decision: we will NOT store card numbers. Use Razorpay tokenization to stay out of PCI scope.",
                  "Need product sign-off on whether saved cards are MVP or phase 2."],
    "REP_SLOW": ["Enterprise tenants have 50k+ rows. The widget queries are not paginated and do full table scans."],
    "PAY_UPI": ["Waiting on Razorpay sandbox approval before we can test end-to-end."],
}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def adf(text: str) -> dict:
    """Tiny markup -> ADF (headings, bullets, paragraphs)."""
    content, bullets = [], []

    def flush():
        if bullets:
            content.append({"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph",
                                                  "content": [{"type": "text", "text": b}]}]}
                for b in bullets]})
            bullets.clear()

    for line in (text or "").splitlines():
        line = line.rstrip()
        if line.startswith("- "):
            bullets.append(line[2:])
            continue
        flush()
        if line.startswith("## "):
            content.append({"type": "heading", "attrs": {"level": 2},
                            "content": [{"type": "text", "text": line[3:]}]})
        elif line:
            content.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
    flush()
    return {"type": "doc", "version": 1, "content": content}


def api(method: str, path: str, **kw):
    r = S.request(method, f"{BASE}{path}", timeout=30, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
    return r.json() if r.text else {}


def issue_types() -> dict:
    """Map our names (epic/story/task/bug/subtask) to this project's real type names."""
    data = api("GET", f"/rest/api/3/issue/createmeta/{PROJECT}/issuetypes")
    types = data.get("issueTypes") or data.get("values") or []
    by_name = {t["name"].lower(): t["name"] for t in types}
    out = {}
    for want in ("epic", "story", "task", "bug"):
        if want in by_name:
            out[want] = by_name[want]
    sub = next((t["name"] for t in types if t.get("subtask")), None)
    if sub:
        out["subtask"] = sub
    out.setdefault("story", out.get("task"))
    out.setdefault("bug", out.get("task"))
    print("Issue types in project:", out)
    return out


def existing_seeded() -> dict:
    """summary -> key for issues already created by this script."""
    found, token = {}, None
    while True:
        params = {"jql": f'project = "{PROJECT}" AND labels = "{LABEL}"',
                  "fields": "summary", "maxResults": 100}
        if token:
            params["nextPageToken"] = token
        data = api("GET", "/rest/api/3/search/jql", params=params)
        for i in data.get("issues", []):
            found[i["fields"]["summary"]] = i["key"]
        token = data.get("nextPageToken")
        if not token:
            return found


def create(fields: dict) -> str:
    try:
        return api("POST", "/rest/api/3/issue", json={"fields": fields})["key"]
    except RuntimeError as e:
        if "priority" in str(e) and "priority" in fields:  # field not on screen in this project
            fields = {k: v for k, v in fields.items() if k != "priority"}
            return api("POST", "/rest/api/3/issue", json={"fields": fields})["key"]
        raise


# ----------------------------------------------------------------------
def seed() -> None:
    types = issue_types()
    have = existing_seeded()
    keys: dict = {}

    def make(item, type_key, parent_ref=None):
        if item["summary"] in have:
            keys[item["ref"]] = have[item["summary"]]
            print(f"  = {keys[item['ref']]:<8} (exists) {item['summary']}")
            return
        fields = {
            "project": {"key": PROJECT},
            "summary": item["summary"],
            "issuetype": {"name": types[type_key]},
            "labels": [LABEL],
        }
        if item.get("desc"):
            fields["description"] = adf(item["desc"])
        if item.get("priority"):
            fields["priority"] = {"name": item["priority"]}
        if parent_ref:
            fields["parent"] = {"key": keys[parent_ref]}
        keys[item["ref"]] = create(fields)
        print(f"  + {keys[item['ref']]:<8} {item['summary']}")

    print("\nEpics")
    for e in EPICS:
        make(e, "epic")
    print("\nIssues")
    for i in ISSUES:
        make(i, i["type"], i.get("epic"))
    print("\nSubtasks")
    for s in SUBTASKS:
        make(s, "subtask", s["parent"])

    if have:
        print("\nSome issues already existed - skipping links/comments to avoid duplicates.")
        print("Run with --cleanup first for a completely fresh seed.")
        return

    print("\nLinks")
    for blocker, blocked in BLOCKS:
        # In the create-link API, inwardIssue is the one that "blocks".
        api("POST", "/rest/api/3/issueLink", json={
            "type": {"name": "Blocks"},
            "inwardIssue": {"key": keys[blocker]},
            "outwardIssue": {"key": keys[blocked]},
        })
        print(f"  {keys[blocker]} blocks {keys[blocked]}")

    print("\nComments")
    for ref, bodies in COMMENTS.items():
        for body in bodies:
            api("POST", f"/rest/api/3/issue/{keys[ref]}/comment", json={"body": adf(body)})
        print(f"  {keys[ref]}: {len(bodies)} comment(s)")

    print(f"\n✓ Seeded {len(keys)} issues into {PROJECT}.")
    print(f"  Check one link in the browser: {BASE}/browse/{keys['PAY_UPI']} "
          f"should say 'is blocked by {keys['PAY_GATEWAY']}'.")


def cleanup() -> None:
    have = existing_seeded()
    if not have:
        print("Nothing to delete.")
        return
    for summary, key in have.items():
        try:
            S.delete(f"{BASE}/rest/api/3/issue/{key}", params={"deleteSubtasks": "true"},
                     timeout=30).raise_for_status()
            print(f"  - {key} {summary}")
        except requests.HTTPError:
            pass  # subtask already removed with its parent
    print(f"✓ Deleted seeded issues from {PROJECT}.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--cleanup", action="store_true")
    args = p.parse_args()
    try:
        cleanup() if args.cleanup else seed()
    except RuntimeError as e:
        sys.exit(f"\n✗ {e}")