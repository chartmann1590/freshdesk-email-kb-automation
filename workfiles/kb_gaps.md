# KB Authoring Backlog

## #127 — E2E gap test: deprecated v1 SOAP endpoint migration timeline
- Resolved: 2026-04-19
- Tags: unrouted, integration-help
- Group: 68000006460
- Resolution excerpt: Hey, Thanks for reaching out. The v1 SOAP endpoint /soap/legacy_dispatch is scheduled for end-of-life on 2026-09-30. Until then it remains in maintenance mode and only receives security patches; no new features are being backported. The recommended migration path is our gRPC dispatch service at dispatch.soulshine.ai:443 with the proto definitions published at https://dispatch.soulshine.ai/proto. Feature parity is guaranteed for all stable v1 methods; three experimental v1 methods (sendEcho, dryRunEnqueue, listLegacyJobs) are not carried forward and have direct equivalents in the new service. R...

