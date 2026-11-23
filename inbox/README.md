# Inbox: raw feedback drop zone

Drop `.json` or `.jsonl` feedback exports here for the next local cycle. The
orchestrator reads this directory (and `state/inbox/`) during `--cycle`,
`--evolve`, and `--replicate`.

**Everything here except this README is gitignored.** Raw feedback may contain
personal data and must never be committed. Ingestion redacts secrets and
sanitizes PII before records reach `state/`.

Recommended record shape (all fields optional except title or body):

```json
{
  "id": "optional-stable-id",
  "source": "customer | support | github_issue | inbox",
  "title": "Short summary",
  "body": "Details",
  "labels": ["customer", "bug"],
  "reactions": 0,
  "created_at": "2026-11-22T12:00:00Z"
}
```

A sanitized demo export lives in `examples/feedback/sample-feedback.json`. Do not
copy real customer exports into the repository; keep them local.
