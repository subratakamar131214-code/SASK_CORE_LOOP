# Human approvals for swarm replication

Replication is disabled by default (`config/system.yaml: swarm.enabled: false`).
Enabling it is a human-reviewed code change. Every child spawn additionally
requires a committed approval file from this directory.

Required fields:

```yaml
action: replication
approved_by: your-github-handle   # a human; bot identities are rejected
reference: https://github.com/OWNER/SASK_CORE_LOOP/issues/123
approved_at: 2026-12-01T00:00:00Z
```

Rules:

- Approvals are committed through a reviewed pull request so the audit trail is
  public. Do not create them from automation.
- Bot identities (`[bot]`, `github-actions`, `dependabot`, `renovate-bot`,
  `actions-bot`) are rejected by the invariant guard.
- The `swarm-replicate` workflow must be dispatched manually, uses the
  `swarm-approval` environment, and should be protected with required reviewers.
- Repository creation (`mode: repo`) also requires `apply: true` and a
  fine-grained `FINE_GRAINED_GH_TOKEN` secret scoped to repository creation only.
- The child pins the parent invariants digest, locks exactly one niche, and may
  not override invariants or enable paid resources.

`example-approval.yaml` is a template and is explicitly rejected by the workflow.
Copy it to a dated file, fill in a real human and issue reference, and open a PR.
