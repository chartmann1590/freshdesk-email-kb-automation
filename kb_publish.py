"""
Freshdesk Solutions article publisher used by hosted automations.

Pushes generated reports to the agent-only "Support Reports" folder under
"Internal: Operations" so agents can read them inside Freshdesk.

Required environment variables:
    FRESHDESK_API_KEY

Optional environment variables:
    FRESHDESK_DOMAIN=soulshineai
"""

from __future__ import annotations

import os
import sys
from typing import Iterable

import requests


def _client():
    api_key = os.environ.get("FRESHDESK_API_KEY", "").strip()
    domain = os.environ.get("FRESHDESK_DOMAIN", "soulshineai").strip()
    if not api_key:
        raise RuntimeError("FRESHDESK_API_KEY missing")
    return f"https://{domain}.freshdesk.com/api/v2", (api_key, "X")


def update_article(article_id: int, title: str, html_body: str, tags: Iterable[str] = ()) -> None:
    base, auth = _client()
    payload = {
        "title": title,
        "description": html_body,
        "status": 2,
        "tags": list(tags),
    }
    r = requests.put(f"{base}/solutions/articles/{article_id}", auth=auth, json=payload, timeout=60)
    r.raise_for_status()
    print(f"[kb_publish] updated article {article_id}: {title}", flush=True)


def create_article(folder_id: int, title: str, html_body: str, tags: Iterable[str] = ()) -> int:
    base, auth = _client()
    payload = {
        "title": title,
        "description": html_body,
        "status": 2,
        "tags": list(tags),
    }
    r = requests.post(f"{base}/solutions/folders/{folder_id}/articles", auth=auth, json=payload, timeout=60)
    r.raise_for_status()
    article_id = r.json()["id"]
    print(f"[kb_publish] created article {article_id}: {title}", flush=True)
    return article_id


if __name__ == "__main__":
    print("kb_publish.py is a library; import update_article / create_article", file=sys.stderr)
    sys.exit(2)
