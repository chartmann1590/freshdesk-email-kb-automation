"""
Weekly support-desk metrics snapshot for SoulShine Freshdesk.

Triggered by a Monday cron in GitHub Actions (or by repository_dispatch
during testing). Pulls the last 7 days of tickets, computes aggregates,
writes a markdown report to workfiles/reports/, and posts a new article
into the agent-only "Support Reports" folder in Freshdesk Solutions.

Required environment variables:
    FRESHDESK_API_KEY
    INTERNAL_REPORTS_FOLDER_ID

Optional environment variables:
    FRESHDESK_DOMAIN=soulshineai
    METRICS_WINDOW_DAYS=7
"""

from __future__ import annotations

import html as html_lib
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import requests

import kb_publish


HERE = Path(__file__).resolve().parent
WORKFILES = HERE / "workfiles"
REPORTS_DIR = WORKFILES / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
SPIKE_ALERTS_FILE = WORKFILES / "spike_alerts.md"
KB_GAPS_FILE = WORKFILES / "kb_gaps.md"

GROUP_NAMES = {
    68000006460: "Technical Support",
    68000006461: "Account & Billing",
    68000006462: "AI Assistant Support",
}
SOURCE_NAMES = {1: "email", 2: "portal", 3: "phone", 7: "chat", 9: "feedback widget", 10: "outbound email"}
STATUS_NAMES = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed", 6: "Waiting on Customer", 7: "Waiting on Third Party"}


def log(message: str) -> None:
    print(f"[metrics] {message}", flush=True)


def fetch_tickets_window(api_key: str, domain: str, window_start_iso: str) -> List[dict]:
    base = f"https://{domain}.freshdesk.com/api/v2"
    auth = (api_key, "X")
    tickets = []
    page = 1
    while True:
        params = {"updated_since": window_start_iso, "per_page": 100, "page": page, "include": "stats"}
        r = requests.get(f"{base}/tickets", auth=auth, params=params, timeout=60)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        tickets.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        if page > 30:
            log("hit page cap (30); stopping pagination")
            break
    return tickets


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def humanize_minutes(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value < 60:
        return f"{value:.0f}m"
    hours = value / 60
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def count_spike_alerts_in_window(window_start: datetime) -> int:
    if not SPIKE_ALERTS_FILE.exists():
        return 0
    count = 0
    for line in SPIKE_ALERTS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        ts_str = line[2:].split(" |", 1)[0].strip()
        ts = parse_iso(ts_str)
        if ts and ts >= window_start:
            count += 1
    return count


def count_new_kb_gaps_in_window(window_start: datetime) -> int:
    if not KB_GAPS_FILE.exists():
        return 0
    count = 0
    for line in KB_GAPS_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("- Resolved: "):
            raw = line.split("Resolved: ", 1)[1].strip()
            ts = parse_iso(raw)
            if ts is None:
                try:
                    ts = datetime.strptime(raw, "%Y-%m-%d")
                except ValueError:
                    continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= window_start:
                count += 1
    return count


def build_report(tickets: List[dict], window_start: datetime, window_end: datetime, week_label: str) -> tuple[str, str]:
    total = len(tickets)
    by_source = Counter(SOURCE_NAMES.get(t.get("source"), str(t.get("source"))) for t in tickets)
    by_group = Counter(GROUP_NAMES.get(t.get("group_id"), f"group:{t.get('group_id')}") for t in tickets)
    by_status = Counter(STATUS_NAMES.get(t.get("status"), str(t.get("status"))) for t in tickets)
    tag_counter = Counter()
    for t in tickets:
        for tag in (t.get("tags") or []):
            tag_counter[tag] += 1

    auto_replied = sum(1 for t in tickets if "ai-kb-auto-replied" in (t.get("tags") or []))
    email_tickets = [t for t in tickets if t.get("source") == 1]
    auto_reply_rate = (auto_replied / len(email_tickets) * 100) if email_tickets else 0

    fr_minutes = []
    res_minutes = []
    reopens = 0
    for t in tickets:
        stats = t.get("stats") or {}
        created = parse_iso(t.get("created_at"))
        first = parse_iso(stats.get("first_responded_at"))
        resolved = parse_iso(stats.get("resolved_at"))
        if created and first:
            fr_minutes.append((first - created).total_seconds() / 60)
        if created and resolved:
            res_minutes.append((resolved - created).total_seconds() / 60)
        if stats.get("reopened_at"):
            reopens += 1

    fr_median = percentile(fr_minutes, 0.5)
    fr_p90 = percentile(fr_minutes, 0.9)
    res_median = percentile(res_minutes, 0.5)
    res_p90 = percentile(res_minutes, 0.9)
    reopen_rate = (reopens / total * 100) if total else 0

    spike_count = count_spike_alerts_in_window(window_start)
    new_gaps = count_new_kb_gaps_in_window(window_start)

    md = []
    md.append(f"# Weekly Support Metrics — {week_label}\n")
    md.append(f"Window: {window_start.strftime('%Y-%m-%d')} → {window_end.strftime('%Y-%m-%d')} (UTC)\n")
    md.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
    md.append("## Summary\n")
    md.append(f"- Total tickets touched in window: **{total}**\n")
    md.append(f"- Email tickets: **{len(email_tickets)}** ({by_source.get('email', 0)})\n")
    md.append(f"- KB auto-reply hit rate (email tickets): **{auto_reply_rate:.0f}%** ({auto_replied}/{len(email_tickets)})\n")
    md.append(f"- Reopen rate: **{reopen_rate:.0f}%** ({reopens}/{total})\n")
    md.append(f"- First response: median **{humanize_minutes(fr_median)}**, p90 **{humanize_minutes(fr_p90)}**\n")
    md.append(f"- Resolution: median **{humanize_minutes(res_median)}**, p90 **{humanize_minutes(res_p90)}**\n")
    md.append(f"- Spike alerts fired this week: **{spike_count}**\n")
    md.append(f"- New KB gaps logged this week: **{new_gaps}**\n\n")

    md.append("## Volume by Group\n")
    for group, count in by_group.most_common():
        md.append(f"- {group}: {count}\n")
    md.append("\n## Volume by Source\n")
    for source, count in by_source.most_common():
        md.append(f"- {source}: {count}\n")
    md.append("\n## Status Mix at Snapshot Time\n")
    for status, count in by_status.most_common():
        md.append(f"- {status}: {count}\n")
    md.append("\n## Top 10 Tags\n")
    for tag, count in tag_counter.most_common(10):
        md.append(f"- `{tag}`: {count}\n")

    md_text = "".join(md)

    # HTML rendering for Freshdesk
    h = ['<div style="font-family:system-ui,Segoe UI,sans-serif">']
    h.append(f"<p><em>Window: {window_start.strftime('%Y-%m-%d')} → {window_end.strftime('%Y-%m-%d')} (UTC). ")
    h.append(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.</em></p>")
    h.append("<h2>Summary</h2><ul>")
    h.append(f"<li>Total tickets touched in window: <strong>{total}</strong></li>")
    h.append(f"<li>Email tickets: <strong>{len(email_tickets)}</strong></li>")
    h.append(f"<li>KB auto-reply hit rate (email tickets): <strong>{auto_reply_rate:.0f}%</strong> ({auto_replied}/{len(email_tickets)})</li>")
    h.append(f"<li>Reopen rate: <strong>{reopen_rate:.0f}%</strong> ({reopens}/{total})</li>")
    h.append(f"<li>First response: median <strong>{humanize_minutes(fr_median)}</strong>, p90 <strong>{humanize_minutes(fr_p90)}</strong></li>")
    h.append(f"<li>Resolution: median <strong>{humanize_minutes(res_median)}</strong>, p90 <strong>{humanize_minutes(res_p90)}</strong></li>")
    h.append(f"<li>Spike alerts fired this week: <strong>{spike_count}</strong></li>")
    h.append(f"<li>New KB gaps logged this week: <strong>{new_gaps}</strong></li>")
    h.append("</ul>")

    def section_table(title, rows):
        h.append(f"<h2>{title}</h2><table border='1' cellpadding='6' cellspacing='0'><tr><th>Name</th><th>Count</th></tr>")
        for name, count in rows:
            h.append(f"<tr><td>{html_lib.escape(str(name))}</td><td>{count}</td></tr>")
        h.append("</table>")

    section_table("Volume by Group", by_group.most_common())
    section_table("Volume by Source", by_source.most_common())
    section_table("Status Mix", by_status.most_common())
    section_table("Top 10 Tags", tag_counter.most_common(10))
    h.append("</div>")
    html_text = "".join(h)
    return md_text, html_text


def main() -> int:
    api_key = os.environ.get("FRESHDESK_API_KEY", "").strip()
    domain = os.environ.get("FRESHDESK_DOMAIN", "soulshineai").strip()
    folder_raw = os.environ.get("INTERNAL_REPORTS_FOLDER_ID", "").strip()
    window_days = int(os.environ.get("METRICS_WINDOW_DAYS", "7"))
    if not (api_key and folder_raw):
        log("missing FRESHDESK_API_KEY / INTERNAL_REPORTS_FOLDER_ID")
        return 1

    folder_id = int(folder_raw)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)

    tickets = fetch_tickets_window(api_key, domain, window_start.isoformat(timespec="seconds"))
    log(f"fetched {len(tickets)} tickets in window")

    iso_year, iso_week, _ = now.isocalendar()
    week_label = f"{iso_year}-W{iso_week:02d}"
    md, html_body = build_report(tickets, window_start, now, week_label)

    out_path = REPORTS_DIR / f"{week_label}.md"
    out_path.write_text(md, encoding="utf-8")
    log(f"wrote report to {out_path}")

    title = f"Weekly Support Metrics — {week_label}"
    kb_publish.create_article(folder_id, title, html_body, tags=["internal-report", "weekly-metrics"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
