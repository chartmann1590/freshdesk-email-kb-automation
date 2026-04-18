# Freshdesk Email KB Automation

Hosted Freshdesk email auto-reply runner for the SoulShine support desk.

This repository is deployed as a scheduled GitHub Actions workflow. It:

- scans recent open email tickets
- finds the best published KB article matches
- replies once on initial contact only
- persists reply state and KB cache in `workfiles/`

The Freshdesk API key is stored as the repository secret `FRESHDESK_API_KEY`.
