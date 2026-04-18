# Freshdesk Email KB Automation

Hosted Freshdesk email auto-reply runner for the SoulShine support desk.

This repository is deployed as a Freshdesk-triggered GitHub Actions workflow with a scheduled fallback. It:

- accepts ticket-specific `repository_dispatch` events from Freshdesk
- finds the best published KB article matches using chunked hybrid retrieval
- replies once on initial contact only
- persists reply state and KB cache in `workfiles/`

The Freshdesk API key is stored as the repository secret `FRESHDESK_API_KEY`.
