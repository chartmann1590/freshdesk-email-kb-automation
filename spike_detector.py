"""
Cross-ticket spike detector for SoulShine Freshdesk.

Triggered by a Freshdesk ticket-creation webhook (repository_dispatch).
Maintains a rolling 60-minute window of recent ticket events per tag/group
and raises an alert when a configured threshold is crossed inside that window.

Outputs:
  - workfiles/spike_state.json: rolling event window (committed back by workflow)
  - workfiles/spike_alerts.md: append-only audit log of spike alerts
  - Freshdesk ticket tag `spike-trigger` on the ticket that crossed a threshold
  - Optional POST to OPS_WEBHOOK_URL (Slack/Discord-compatible JSON)

Required environment variables:
    FRESHDESK_API_KEY
    TARGET_TICKET_ID

Optional environment variables:
    FRESHDESK_DOMAIN=soulshineai
    OPS_WEBHOOK_URL=https://hooks.slack.com/...
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import requests


HERE = Path(__file__).resolve().parent
WORKFILES = HERE / "workfiles"
WORKFILES.mkdir(exist_ok=True)
STATE_FILE = WORKFILES / "spike_state.json"
ALERTS_FILE = WORKFILES / "spike_alerts.md"

WINDOW_SECONDS = 60 * 60

# Threshold = N tickets sharing the same signal inside WINDOW_SECONDS.
# Tune these as you learn real volumes; start conservative.
TAG_THRESHOLDS: Dict[str, int] = {
    "bug": 5,
    "access-issue": 4,
    "churn-risk": 3,
    "integration-help": 5,
    "crisis": 2,
}

GROUP_THRESHOLDS: Dict[str, int] = {
    "Technical Support": 8,
    "Account & Billing": 6,
    "AI Assistant Support": 6,
}

SPIKE_TAG = "spike-trigger"
ALERT_COOLDOWN_SECONDS = 30 * 60  # don't re-alert on the same signal within 30 min


def log(message: str) -> None:
    print(f"[spike] {message}", flush=True)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"events": [], "last_alert": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log("state file unreadable, resetting")
        return {"events": [], "last_alert": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def prune_window(events: List[dict], now: float) -> List[dict]:
    cutoff = now - WINDOW_SECONDS
    return [event for event in events if event["ts"] >= cutoff]


def fetch_ticket(api_key: str, domain: str, ticket_id: int) -> dict:
    url = f"https://{domain}.freshdesk.com/api/v2/tickets/{ticket_id}"
    response = requests.get(url, auth=(api_key, "X"), timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_group_name(api_key: str, domain: str, group_id: int) -> str:
    if not group_id:
        return ""
    url = f"https://{domain}.freshdesk.com/api/v2/groups/{group_id}"
    response = requests.get(url, auth=(api_key, "X"), timeout=30)
    if response.status_code != 200:
        return ""
    return response.json().get("name", "")


def add_spike_tag(api_key: str, domain: str, ticket: dict) -> None:
    existing_tags = list(ticket.get("tags") or [])
    if SPIKE_TAG in existing_tags:
        return
    existing_tags.append(SPIKE_TAG)
    url = f"https://{domain}.freshdesk.com/api/v2/tickets/{ticket['id']}"
    response = requests.put(
        url,
        auth=(api_key, "X"),
        json={"tags": existing_tags},
        timeout=30,
    )
    response.raise_for_status()


def post_internal_note(api_key: str, domain: str, ticket_id: int, body: str) -> None:
    url = f"https://{domain}.freshdesk.com/api/v2/tickets/{ticket_id}/notes"
    response = requests.post(
        url,
        auth=(api_key, "X"),
        json={"body": body, "private": True},
        timeout=30,
    )
    response.raise_for_status()


def post_ops_webhook(webhook_url: str, payload: dict) -> None:
    try:
        requests.post(webhook_url, json=payload, timeout=15)
    except requests.RequestException as exc:
        log(f"ops webhook post failed: {exc}")


def append_alert(signal: str, count: int, window_minutes: int, ticket_id: int, tickets: List[int]) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = (
        f"- {ts} | signal=`{signal}` | count={count} in {window_minutes}m "
        f"| trigger=#{ticket_id} | window_tickets={tickets}\n"
    )
    if not ALERTS_FILE.exists():
        ALERTS_FILE.write_text("# Spike Alerts\n\n", encoding="utf-8")
    with ALERTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line)


def evaluate_signal(
    signal: str,
    threshold: int,
    events: List[dict],
    state: dict,
    now: float,
) -> List[int]:
    matching = [event for event in events if signal in event.get("signals", [])]
    if len(matching) < threshold:
        return []
    last = state["last_alert"].get(signal, 0)
    if now - last < ALERT_COOLDOWN_SECONDS:
        return []
    state["last_alert"][signal] = now
    return [event["ticket_id"] for event in matching]


def main() -> int:
    api_key = os.environ.get("FRESHDESK_API_KEY", "").strip()
    domain = os.environ.get("FRESHDESK_DOMAIN", "soulshineai").strip()
    target_raw = os.environ.get("TARGET_TICKET_ID", "").strip()
    if not api_key:
        log("FRESHDESK_API_KEY missing")
        return 1
    if not target_raw:
        log("TARGET_TICKET_ID missing; nothing to evaluate")
        return 0

    ticket_id = int(target_raw)
    ticket = fetch_ticket(api_key, domain, ticket_id)

    tags = [tag for tag in (ticket.get("tags") or []) if tag in TAG_THRESHOLDS]
    group_name = fetch_group_name(api_key, domain, ticket.get("group_id") or 0)
    group_signal = f"group:{group_name}" if group_name in GROUP_THRESHOLDS else ""
    signals = list(tags)
    if group_signal:
        signals.append(group_signal)

    log(f"ticket #{ticket_id} signals={signals}")

    state = load_state()
    now = time.time()
    state["events"] = prune_window(state.get("events", []), now)
    state["events"].append({"ticket_id": ticket_id, "ts": now, "signals": signals})

    triggered = []
    for tag, threshold in TAG_THRESHOLDS.items():
        window_tickets = evaluate_signal(tag, threshold, state["events"], state, now)
        if window_tickets:
            triggered.append((tag, threshold, window_tickets))

    for group_name, threshold in GROUP_THRESHOLDS.items():
        signal = f"group:{group_name}"
        window_tickets = evaluate_signal(signal, threshold, state["events"], state, now)
        if window_tickets:
            triggered.append((signal, threshold, window_tickets))

    save_state(state)

    if not triggered:
        log("no spike thresholds crossed")
        return 0

    webhook_url = os.environ.get("OPS_WEBHOOK_URL", "").strip()
    note_lines = ["**Spike detected by hosted automation.**", ""]
    for signal, threshold, window_tickets in triggered:
        log(f"SPIKE: {signal} hit {len(window_tickets)} (threshold {threshold})")
        append_alert(signal, len(window_tickets), WINDOW_SECONDS // 60, ticket_id, window_tickets)
        note_lines.append(
            f"- `{signal}`: {len(window_tickets)} tickets in {WINDOW_SECONDS // 60}m "
            f"(threshold {threshold}). Recent: {window_tickets}"
        )
        if webhook_url:
            post_ops_webhook(
                webhook_url,
                {
                    "text": (
                        f":rotating_light: Freshdesk spike: `{signal}` hit "
                        f"{len(window_tickets)} in {WINDOW_SECONDS // 60}m "
                        f"(threshold {threshold}). Trigger ticket #{ticket_id}."
                    )
                },
            )

    add_spike_tag(api_key, domain, ticket)
    post_internal_note(api_key, domain, ticket_id, "\n".join(note_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
