# Security

The system is designed so that a mistake in automation cannot spend money,
publish customer data, or reveal credentials. This document describes the
controls, the threat model, and the incident steps.

## Credentials

### Where credentials live

Only in **GitHub Actions Secrets** (repository or environment scope), injected as
environment variables at runtime:

| Secret | Used for | Scope |
| --- | --- | --- |
| `GROQ_API_KEY` | Free-tier Groq inference | `revenue-cycle` only |
| `GEMINI_API_KEY` | Free-tier Gemini fallback | `revenue-cycle` only |
| `FINE_GRAINED_GH_TOKEN` | Approved child repository creation | `swarm-replicate` only |

Locally, export the same variables in your shell. Nothing auto-loads `.env`;
`.env.example` is documentation. `.gitignore` excludes `.env*` (except the
example), private keys, `secrets/`, and keystores.

### Never

- Never commit a key, token, cookie, private key, or payment credential.
- Never paste one into an issue, PR description, chat, workflow log, or artifact.
- Never place credentials in `config/`, `state/`, `docs/`, tests, or generated
  products.
- Never widen the fine-grained token beyond creating public repositories needed
  for approved children.

### If a credential leaks

1. **Revoke it first** in the provider console (GitHub token, Groq, Google AI
   Studio). Do not wait for cleanup.
2. Issue a new credential with the minimum scope and store it only as a GitHub
   Actions Secret.
3. Remove it from the working tree and from any logs/artifacts. Use
   `git filter-repo` or GitHub secret scanning guidance if it reached history;
   coordinate a rewrite with all collaborators.
4. Assume any credential pasted into a chat, document, or ticket is compromised
   even if it "looks like a placeholder." Placeholders in this repository are
   deliberately fake and are never real values.
5. Record the incident in a private security note; do not publish rotated values.

## Secret scanning

`python -m src.compliance.secretscan` fails closed on:

- Provider token shapes (GitHub, Groq, Google, AWS, Slack, Stripe-style keys)
- JWT and authorization headers
- Private key blocks and URL userinfo
- Sensitive assignments (`password=`, `api_key:`, etc.)
- High-entropy 32+ character values on lines with secret-like context
  (excluding pure hex, which is usually a digest)

It runs on state immediately before a CI commit, on the full tree in `verify`,
and can scan a file or directory locally. Binary files and common build/VCS
directories are skipped. Findings report path, line, and kind — never the value.

## PII and prompt safety

- Feedback ingress redacts emails, phone numbers, SSNs, Luhn-valid card numbers,
  private IPs, Windows/home paths, and common identity fields before storage.
- Sanitization is idempotent and recursive for JSON payloads.
- Outbound inference receives sanitized messages only. `allow_pii_to_cloud` is
  `false` in the invariants and is checked intrinsically.
- Generated landing pages contain product metadata only: no feedback bodies, no
  customer names, no email addresses, no analytics scripts, and no forms.
- Raw inboxes (`inbox/`, `state/inbox/`) are always gitignored.

Residual risk: free-text that does not match a pattern can still be sensitive.
Do not feed regulated or highly sensitive data into the loop. The system is
built for public developer-tool signals.

## Approvals

Human approval is enforced in code for:

- Real-money payouts and payment credentials (no automation exists to perform
  either)
- Swarm replication and child repository creation
- Invariant changes and repository visibility changes

Approval objects require an action, human approver, reference, and ISO timestamp.
Bot identities are rejected. Replication approvals are committed under
`approvals/` and gated again by the `swarm-approval` GitHub Environment.
Even with approval, runtime code can never edit protected paths; those changes
must go through a reviewed human commit.

## Repository integrity

- `config/invariants.yaml` is pinned by SHA-256. A mismatch stops every command.
- Intrinsic checks reject weakened invariants even if a new digest is pinned.
- Protected paths: invariants, redaction, sanitizer, compliance package, and
  workflows. Evolution proposals and child replicas may not target them.
- The ledger hash chain makes silent edits detectable; `--status` and the
  heartbeat report chain validity.

## GitHub Actions hardening

- Explicit `permissions`: `contents: read` for verification, `contents: write`
  only for sanitized state commits.
- One shared concurrency group for state writers prevents ledger races.
- Explicit `timeout-minutes` on every job.
- No larger/paid runners, no paid services, no cloud billing credentials.
- Actions are referenced by major version (`@v4`, `@v5`); pin to commit SHAs if
  your policy requires it.
- Scheduled workflows are subject to GitHub's inactivity and fair-use rules.
- Enable branch protection and required reviews on `main`, and configure
  required reviewers on the `swarm-approval` environment.

## Supply chain

Runtime dependencies: Python 3.11+ and `PyYAML` only. Dev dependencies are
`pytest`, `ruff`, `mypy`, and `types-PyYAML`, installed in CI from PyPI. Generated
products use the Python standard library only and are packaged with their own
lock-free, dependency-free test suite. License allowlisting prevents packaging
under a non-approved license.

## Threat model (selected)

| Threat | Control |
| --- | --- |
| Prompt injection in feedback | Feedback is data, not instructions; sanitized and used only for scoring/titles; model output is sanitized and never executed |
| Accidental spend | $0.00 config, ledger, host allowlist, provider quotas, no paid billing credentials |
| Secret in a commit | Redaction at ingress, sanitized state writes, pre-commit scan, CI full-tree scan |
| PII in a public page | Pages use metadata only; feedback bodies never rendered |
| Tampered history | Hash-chained ledger; chain validation alerts |
| Rogue child replica | Disabled by default; human approval, one-niche lock, digest pin, child cap |
| Self-modifying automation | Protected paths; proposals always `auto_apply: false`; human merge required |
| Concurrent state corruption | Shared workflow concurrency group, atomic writes, file locks |
| Over-broad token | Fine-grained token used only in the manual replication job |

## Reporting

Open a private security advisory on the repository (or contact the maintainer
privately) for suspected leaks or bypasses. Do not include live credentials in
the report; state which secret type was exposed and rotate it immediately.
