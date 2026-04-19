"""
KB authoring backlog logger.

Triggered by a Freshdesk ticket-update webhook fired when a ticket is
resolved. If the ticket lacked an auto-KB-reply and is not in a skip
category (spam, pastoral-care), it's logged as a candidate gap.

Outputs:
  - workfiles/kb_gaps.md: append-only audit log (committed back by workflow)
  - Freshdesk article (KB Authoring Backlog) updated with current backlog

Required environment variables:
    FRESHDESK_API_KEY
    TARGET_TICKET_ID
    KB_BACKLOG_ARTICLE_ID

Optional environment variables:
    FRESHDESK_DOMAIN=soulshineai
"""

from __future__ import annotations

import html as html_lib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests

import kb_publish


HERE = Path(__file__).resolve().parent
WORKFILES = HERE / "workfiles"
WORKFILES.mkdir(exist_ok=True)
GAPS_FILE = WORKFILES / "kb_gaps.md"

SKIP_TAGS = {"ai-kb-auto-replied", "spam", "pastoral-care", "e2e-spike-test"}
MIN_AGENT_REPLY_CHARS = 200


def log(message: str) -> None:
    print(f"[kb-gap] {message}", flush=True)


def fetch(api_key: str, domain: str, path: str) -> dict:
    url = f"https://{domain}.freshdesk.com/api/v2/{path}"
    r = requests.get(url, auth=(api_key, "X"), timeout=30)
    r.raise_for_status()
    return r.json()


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html_lib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def find_resolution_excerpt(conversations: list) -> Optional[str]:
    for conv in reversed(conversations):
        if conv.get("private") or conv.get("incoming"):
            continue
        body = strip_html(conv.get("body_text") or conv.get("body") or "")
        if len(body) >= MIN_AGENT_REPLY_CHARS:
            return body[:600] + ("..." if len(body) > 600 else "")
    return None


def append_entry(ticket: dict, excerpt: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tags = ticket.get("tags") or []
    subject = (ticket.get("subject") or "").replace("|", "-")
    line = (
        f"## #{ticket['id']} — {subject}\n"
        f"- Resolved: {ts}\n"
        f"- Tags: {', '.join(tags) if tags else '(none)'}\n"
        f"- Group: {ticket.get('group_id')}\n"
        f"- Resolution excerpt: {excerpt}\n\n"
    )
    if not GAPS_FILE.exists():
        GAPS_FILE.write_text("# KB Authoring Backlog\n\n", encoding="utf-8")
    with GAPS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line)


def render_html() -> str:
    if not GAPS_FILE.exists():
        body_md = "_No KB gaps logged yet._"
    else:
        body_md = GAPS_FILE.read_text(encoding="utf-8")

    parts = ['<div style="font-family:system-ui,Segoe UI,sans-serif">']
    parts.append("<p><em>Auto-generated from resolved tickets that did not match an existing KB article. ")
    parts.append("Each entry below is a candidate for a new KB article. ")
    parts.append(f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.</em></p>")
    parts.append("<hr/>")

    for line in body_md.splitlines():
        if line.startswith("# "):
            parts.append(f"<h1>{html_lib.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            parts.append(f"<h2>{html_lib.escape(line[3:])}</h2>")
        elif line.startswith("- "):
            parts.append(f"<p>{html_lib.escape(line[2:])}</p>")
        elif line.strip():
            parts.append(f"<p>{html_lib.escape(line)}</p>")
    parts.append("</div>")
    return "".join(parts)


def main() -> int:
    api_key = os.environ.get("FRESHDESK_API_KEY", "").strip()
    domain = os.environ.get("FRESHDESK_DOMAIN", "soulshineai").strip()
    target_raw = os.environ.get("TARGET_TICKET_ID", "").strip()
    article_raw = os.environ.get("KB_BACKLOG_ARTICLE_ID", "").strip()
    if not (api_key and target_raw and article_raw):
        log("missing FRESHDESK_API_KEY / TARGET_TICKET_ID / KB_BACKLOG_ARTICLE_ID")
        return 1

    ticket_id = int(target_raw)
    article_id = int(article_raw)

    ticket = fetch(api_key, domain, f"tickets/{ticket_id}")
    tags = set(ticket.get("tags") or [])
    if SKIP_TAGS & tags:
        log(f"ticket #{ticket_id} skipped (tags={sorted(SKIP_TAGS & tags)})")
        return 0
    # Skip the status re-check: this script is invoked by the Freshdesk
    # automation rule that fires on transition to status=4 (Resolved). By the
    # time GitHub Actions runs, other automations (e.g. agent-reply auto-status)
    # may have flipped the status. Trust the trigger.

    conversations = fetch(api_key, domain, f"tickets/{ticket_id}/conversations")
    excerpt = find_resolution_excerpt(conversations)
    if not excerpt:
        log(f"ticket #{ticket_id} has no qualifying agent reply (>{MIN_AGENT_REPLY_CHARS} chars); skipping")
        return 0

    append_entry(ticket, excerpt)
    log(f"ticket #{ticket_id} logged as KB gap")

    kb_publish.update_article(
        article_id,
        title="KB Authoring Backlog (Live)",
        html_body=render_html(),
        tags=["internal-report", "kb-gaps"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
