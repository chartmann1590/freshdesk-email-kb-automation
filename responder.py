"""
Hosted Freshdesk email auto-responder backed by the live SoulShine KB.

This runner is designed for a scheduled hosted environment such as GitHub
Actions. It scans recent open email tickets, finds the best KB matches, and
posts a public reply through the Freshdesk API on first contact only.

Required environment variables:
    FRESHDESK_API_KEY

Optional environment variables:
    FRESHDESK_DOMAIN=soulshineai
    RESPONDER_LOG_FILE=/path/to/logfile
    TARGET_TICKET_ID=123
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel


HERE = Path(__file__).resolve().parent
WORKFILES = HERE / "workfiles"
WORKFILES.mkdir(exist_ok=True)
STATE_FILE = WORKFILES / "email_ai_responder_state.json"
KB_CACHE_FILE = WORKFILES / "email_ai_responder_kb_cache.json"
LOG_FILE = os.environ.get("RESPONDER_LOG_FILE", "").strip()

EMAIL_SOURCE_ID = 1
PROCESSED_TAG = "ai-kb-auto-replied"
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "how",
    "i",
    "if",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "please",
    "that",
    "the",
    "this",
    "to",
    "we",
    "what",
    "when",
    "where",
    "with",
    "you",
    "your",
}
QUERY_NORMALIZATIONS = (
    ("log in", "login"),
    ("sign in", "login"),
    ("sign-in", "login"),
    ("sign on", "login"),
    ("sign-on", "login"),
    ("username", "user name"),
    ("e-mail", "email"),
    ("alert emails", "notifications"),
    ("alert email", "notification"),
    ("email alerts", "notifications"),
    ("notification emails", "notifications"),
    ("notification email", "notification"),
    ("weekly digest", "digest frequency"),
    ("digest emails", "digest frequency"),
    ("digest email", "digest frequency"),
    ("display name", "profile"),
    ("account details", "profile"),
    ("account page", "account settings profile"),
    ("personal details", "profile"),
    ("account info", "profile"),
    ("remove my account", "delete account"),
    ("erase my account", "delete account"),
    ("download my data", "data export"),
    ("export my data", "data export"),
    ("turn off", "disable"),
    ("shut off", "disable"),
)
WORD_VECTOR_WEIGHT = 0.72
CHAR_VECTOR_WEIGHT = 0.28


def load_config() -> tuple[str, tuple[str, str], dict]:
    api_key = os.environ.get("FRESHDESK_API_KEY", "").strip()
    if not api_key:
        api_file = HERE / "API.txt"
        if api_file.exists():
            api_key = api_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise RuntimeError("Freshdesk API key not found. Set FRESHDESK_API_KEY.")

    domain = os.environ.get("FRESHDESK_DOMAIN", "soulshineai").strip()
    base_url = f"https://{domain}.freshdesk.com/api/v2"
    auth = (api_key, "X")
    headers = {"Content-Type": "application/json"}
    return base_url, auth, headers


BASE_URL, AUTH, HEADERS = load_config()


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    line = f"[{timestamp}] {message}"
    print(line)
    if LOG_FILE:
        path = Path(LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def strip_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def slug_words(text: str) -> List[str]:
    tokens = re.findall(r"[a-z0-9']+", text.lower())
    return [token for token in tokens if len(token) > 1 and token not in STOPWORDS]


def normalize_for_matching(text: str) -> str:
    normalized = strip_html(text).lower()
    normalized = normalized.replace("/", " ")
    normalized = normalized.replace("-", " ")
    normalized = normalized.replace("&", " and ")
    for source, target in QUERY_NORMALIZATIONS:
        normalized = normalized.replace(source, target)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def split_into_chunks(text: str, max_sentences: int = 3) -> List[str]:
    clean = strip_html(text)
    if not clean:
        return []

    paragraphs = [part.strip() for part in re.split(r"\n{2,}", clean) if part.strip()]
    if not paragraphs:
        paragraphs = [clean]

    chunks: List[str] = []
    for paragraph in paragraphs:
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", paragraph) if part.strip()]
        if not sentences:
            sentences = [paragraph]

        if len(sentences) <= max_sentences:
            chunks.append(" ".join(sentences).strip())
            continue

        for index in range(0, len(sentences), max_sentences - 1):
            window = sentences[index : index + max_sentences]
            if window:
                chunks.append(" ".join(window).strip())

    deduped: List[str] = []
    seen = set()
    for chunk in chunks:
        key = chunk.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(chunk)
    return deduped


def sentence_excerpt(text: str, limit: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    parts = [part.strip() for part in parts if part.strip()]
    if not parts:
        return ""
    excerpt = " ".join(parts[:limit]).strip()
    return excerpt[:420].rstrip()


class FreshdeskClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.auth = AUTH
        self.session.headers.update(HEADERS)
        self.session.timeout = 30

    def get(self, endpoint: str, params: Optional[dict] = None):
        response = self.session.get(f"{BASE_URL}/{endpoint}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def post(self, endpoint: str, payload: dict):
        response = self.session.post(f"{BASE_URL}/{endpoint}", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def put(self, endpoint: str, payload: dict):
        response = self.session.put(f"{BASE_URL}/{endpoint}", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()


class KBIndex:
    def __init__(self, client: FreshdeskClient) -> None:
        self.client = client
        self.articles: List[dict] = []
        self.chunks: List[dict] = []
        self.loaded_at: Optional[float] = None
        self.word_vectorizer: Optional[TfidfVectorizer] = None
        self.char_vectorizer: Optional[TfidfVectorizer] = None
        self.word_matrix = None
        self.char_matrix = None

    def ensure_loaded(self, max_age_seconds: int = 21600) -> None:
        now = time.time()
        if self.loaded_at and (now - self.loaded_at) < max_age_seconds and self.articles:
            return
        cache_loaded = self._load_cache(max_age_seconds=max_age_seconds)
        if cache_loaded and self.loaded_at and (now - self.loaded_at) < max_age_seconds:
            return

        try:
            self._refresh_live()
        except requests.RequestException as exc:
            if self.articles:
                log(f"KB refresh failed; using cached index ({exc})")
                return
            raise

    def _load_cache(self, max_age_seconds: int) -> bool:
        if not KB_CACHE_FILE.exists():
            return False

        try:
            payload = json.loads(KB_CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False

        cached_at = payload.get("cached_at")
        cached_articles = payload.get("articles") or []
        if not cached_at or not cached_articles:
            return False

        cached_ts = float(cached_at)
        self.articles = []
        for item in cached_articles:
            self.articles.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "body": item["body"],
                    "url": item["url"],
                    "normalized_title": item.get("normalized_title") or normalize_for_matching(item["title"]),
                    "normalized_body": item.get("normalized_body") or normalize_for_matching(item["body"]),
                    "chunks": item.get("chunks") or split_into_chunks(item["body"]),
                }
            )

        self.loaded_at = cached_ts
        self._rebuild_index()
        age = int(time.time() - cached_ts)
        log(f"KB cache loaded: {len(self.articles)} articles ({age}s old)")
        return age < max_age_seconds

    def _save_cache(self) -> None:
        payload = {
            "cached_at": self.loaded_at or time.time(),
            "articles": [
                {
                    "id": article["id"],
                    "title": article["title"],
                    "body": article["body"],
                    "url": article["url"],
                    "normalized_title": article["normalized_title"],
                    "normalized_body": article["normalized_body"],
                    "chunks": article["chunks"],
                }
                for article in self.articles
            ],
        }
        KB_CACHE_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _rebuild_index(self) -> None:
        self.chunks = []
        for article in self.articles:
            normalized_title = article["normalized_title"]
            for chunk_index, chunk_text in enumerate(article["chunks"]):
                normalized_chunk = normalize_for_matching(chunk_text)
                if not normalized_chunk:
                    continue
                self.chunks.append(
                    {
                        "article_id": article["id"],
                        "title": article["title"],
                        "url": article["url"],
                        "chunk_index": chunk_index,
                        "text": chunk_text,
                        "normalized_text": normalized_chunk,
                        "document": f"{normalized_title}. {normalized_chunk}",
                    }
                )

        if not self.chunks:
            self.word_vectorizer = None
            self.char_vectorizer = None
            self.word_matrix = None
            self.char_matrix = None
            return

        documents = [chunk["document"] for chunk in self.chunks]
        self.word_vectorizer = TfidfVectorizer(
            analyzer="word",
            ngram_range=(1, 2),
            min_df=1,
            stop_words="english",
            sublinear_tf=True,
        )
        self.char_vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(3, 5),
            min_df=1,
            sublinear_tf=True,
        )
        self.word_matrix = self.word_vectorizer.fit_transform(documents)
        self.char_matrix = self.char_vectorizer.fit_transform(documents)

    def _refresh_live(self) -> None:
        categories = self.client.get("solutions/categories")
        articles: List[dict] = []

        for category in categories:
            folders = self.client.get(f"solutions/categories/{category['id']}/folders")
            for folder in folders:
                folder_articles = self.client.get(f"solutions/folders/{folder['id']}/articles")
                for article_stub in folder_articles:
                    article = self.client.get(f"solutions/articles/{article_stub['id']}")
                    clean_body = strip_html(article.get("description_text") or article.get("description") or "")
                    clean_title = strip_html(article.get("title") or "")
                    articles.append(
                        {
                            "id": article["id"],
                            "title": clean_title,
                            "body": clean_body,
                            "normalized_title": normalize_for_matching(clean_title),
                            "normalized_body": normalize_for_matching(clean_body),
                            "chunks": split_into_chunks(clean_body),
                            "url": article.get("url")
                            or f"https://{os.environ.get('FRESHDESK_DOMAIN', 'soulshineai')}.freshdesk.com/support/solutions/articles/{article['id']}",
                        }
                    )

        self.articles = articles
        self.loaded_at = time.time()
        self._rebuild_index()
        self._save_cache()
        log(f"KB index loaded: {len(self.articles)} articles")

    def search(self, query_text: str, limit: int = 3) -> List[Tuple[float, dict]]:
        if not self.chunks or self.word_vectorizer is None or self.char_vectorizer is None:
            return []

        normalized_query = normalize_for_matching(query_text)
        query_tokens = slug_words(normalized_query)
        if not query_tokens:
            return []

        word_query = self.word_vectorizer.transform([normalized_query])
        char_query = self.char_vectorizer.transform([normalized_query])
        word_scores = linear_kernel(word_query, self.word_matrix).ravel()
        char_scores = linear_kernel(char_query, self.char_matrix).ravel()

        article_scores: Dict[int, dict] = defaultdict(
            lambda: {
                "title": "",
                "url": "",
                "body": "",
                "chunks": [],
                "best_excerpt": "",
                "best_score": 0.0,
            }
        )
        article_map = {article["id"]: article for article in self.articles}
        query_text_lower = normalized_query.lower()
        query_token_set = set(query_tokens)

        for index, chunk in enumerate(self.chunks):
            chunk_score = (WORD_VECTOR_WEIGHT * float(word_scores[index])) + (
                CHAR_VECTOR_WEIGHT * float(char_scores[index])
            )
            if chunk_score <= 0:
                continue

            title_lower = normalize_for_matching(chunk["title"])
            if title_lower and title_lower in query_text_lower:
                chunk_score += 0.18

            title_overlap = len(query_token_set & set(slug_words(title_lower)))
            if title_overlap:
                chunk_score += min(title_overlap * 0.03, 0.12)

            article_score = article_scores[chunk["article_id"]]
            article_score["title"] = chunk["title"]
            article_score["url"] = chunk["url"]
            article = article_map[chunk["article_id"]]
            article_score["body"] = article["body"]
            article_score["chunks"].append((chunk_score, chunk["text"]))
            if chunk_score > article_score["best_score"]:
                article_score["best_score"] = chunk_score
                article_score["best_excerpt"] = chunk["text"]

        scored: List[Tuple[float, dict]] = []
        for article_id, article in article_scores.items():
            chunk_list = sorted(article["chunks"], key=lambda item: item[0], reverse=True)
            supporting = sum(score for score, _ in chunk_list[:2]) * 0.12
            final_score = article["best_score"] + supporting
            scored.append(
                (
                    final_score,
                    {
                        "id": article_id,
                        "title": article["title"],
                        "body": article["body"],
                        "url": article["url"],
                        "match_excerpt": article["best_excerpt"],
                    },
                )
            )

        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[:limit]


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def recent_open_email_tickets(client: FreshdeskClient, per_page: int = 50) -> List[dict]:
    tickets = client.get(
        "tickets",
        params={"per_page": per_page, "order_by": "created_at", "order_type": "desc"},
    )
    results = []
    for ticket in tickets:
        if ticket.get("status") != 2:
            continue
        if ticket.get("source") != EMAIL_SOURCE_ID:
            continue
        results.append(ticket)
    return results


def fetch_ticket(client: FreshdeskClient, ticket_id: int) -> dict:
    return client.get(f"tickets/{ticket_id}")


def has_agent_conversation(client: FreshdeskClient, ticket_id: int) -> bool:
    conversations = client.get(f"tickets/{ticket_id}/conversations")
    return bool(conversations)


def mark_ticket_processed(state: dict, ticket_id: int, detail: str) -> None:
    state.setdefault("processed", {})[str(ticket_id)] = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "detail": detail,
    }


def already_processed(state: dict, ticket_id: int) -> bool:
    return str(ticket_id) in state.get("processed", {})


def build_reply_body(ticket: dict, matches: List[Tuple[float, dict]]) -> Optional[str]:
    if not matches:
        return None

    best_score = matches[0][0]
    if best_score < 0.22:
        return None

    requester_name = strip_html(ticket.get("requester", {}).get("name") or "there")
    opener = f"<p>Hi {html.escape(requester_name)},</p>"

    if best_score >= 0.55:
        intro = "<p>Your question closely matches an article in our support knowledge base.</p>"
    else:
        intro = "<p>I found the most relevant guidance from our support knowledge base for your question.</p>"

    items = []
    for score, article in matches:
        if score < 0.16:
            continue
        excerpt = sentence_excerpt(article.get("match_excerpt") or article["body"])
        excerpt_html = (
            html.escape(excerpt) if excerpt else "Open the article for the full step-by-step guidance."
        )
        items.append(
            "<li>"
            f"<strong><a href=\"{html.escape(article['url'])}\">{html.escape(article['title'])}</a></strong>"
            f"<br>{excerpt_html}"
            "</li>"
        )

    if not items:
        return None

    close = (
        "<p>If this does not fully solve it, just reply to this email and our team will keep working the ticket with you.</p>"
        "<p>Best regards,<br>SoulShine Support</p>"
    )
    return opener + intro + "<ul>" + "".join(items) + "</ul>" + close


def add_processed_tag(client: FreshdeskClient, ticket: dict) -> None:
    tags = list(ticket.get("tags") or [])
    if PROCESSED_TAG in tags:
        return
    tags.append(PROCESSED_TAG)
    client.put(f"tickets/{ticket['id']}", {"tags": tags})


def process_ticket(client: FreshdeskClient, kb_index: KBIndex, state: dict, ticket: dict) -> bool:
    ticket_id = ticket["id"]
    if already_processed(state, ticket_id):
        return False

    tags = ticket.get("tags") or []
    if PROCESSED_TAG in tags:
        mark_ticket_processed(state, ticket_id, "tag_already_present")
        return False

    if has_agent_conversation(client, ticket_id):
        mark_ticket_processed(state, ticket_id, "conversation_already_present")
        return False

    subject = ticket.get("subject") or ""
    description = strip_html(ticket.get("description_text") or ticket.get("description") or "")
    query = f"{subject}\n{description}".strip()
    matches = kb_index.search(query, limit=3)
    body = build_reply_body(ticket, matches)

    if not body:
        log(f"Skipped ticket #{ticket_id}: no strong KB match")
        mark_ticket_processed(state, ticket_id, "no_strong_match")
        return False

    reply = client.post(f"tickets/{ticket_id}/reply", {"body": body})
    add_processed_tag(client, ticket)
    top_title = matches[0][1]["title"] if matches else "unknown"
    log(f"Replied to ticket #{ticket_id} using KB article '{top_title}' (reply id {reply.get('id')})")
    mark_ticket_processed(state, ticket_id, f"replied:{top_title}")
    return True


def run_once(
    client: FreshdeskClient,
    kb_index: KBIndex,
    state: dict,
    target_ticket_id: Optional[int] = None,
) -> int:
    kb_index.ensure_loaded()
    if target_ticket_id is not None:
        ticket = fetch_ticket(client, target_ticket_id)
        tickets = [ticket]
        log(f"Scanning targeted ticket #{target_ticket_id}")
    else:
        tickets = recent_open_email_tickets(client)
        log(f"Scanning {len(tickets)} recent open email tickets")
    replies = 0
    for ticket in tickets:
        try:
            if ticket.get("status") != 2:
                log(f"Skipped ticket #{ticket.get('id')}: status is {ticket.get('status')}")
                continue
            if ticket.get("source") != EMAIL_SOURCE_ID:
                log(f"Skipped ticket #{ticket.get('id')}: source is {ticket.get('source')}")
                continue
            if process_ticket(client, kb_index, state, ticket):
                replies += 1
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            body = exc.response.text[:500] if exc.response is not None else str(exc)
            log(f"HTTP error on ticket #{ticket.get('id')}: {status} {body}")
        except Exception as exc:  # pragma: no cover
            log(f"Unexpected error on ticket #{ticket.get('id')}: {exc}")
    save_state(state)
    log(f"Run complete: {replies} ticket(s) replied")
    return replies


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hosted SoulShine Freshdesk email KB responder")
    parser.add_argument("command", choices=["once"], help="Run one scan/reply pass")
    parser.add_argument("--ticket-id", type=int, help="Process a specific Freshdesk ticket id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = FreshdeskClient()
    kb_index = KBIndex(client)
    state = load_state()

    if args.command == "once":
        target_ticket_id = args.ticket_id
        if target_ticket_id is None:
            env_ticket_id = os.environ.get("TARGET_TICKET_ID", "").strip()
            if env_ticket_id:
                target_ticket_id = int(env_ticket_id)
        run_once(client, kb_index, state, target_ticket_id=target_ticket_id)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
