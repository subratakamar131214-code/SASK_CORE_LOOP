# SASK_CORE_LOOP

A local-first, **$0.00-billing**, public-repository product factory. It ingests
sanitized demand signals, scores niches, builds and packages small open-core
developer tools, publishes sanitized release artifacts, watches its own health,
and proposes (never applies) improvements — all on free tiers and free
public-repository runners.

This is not "infinite compute". It is a bounded, auditable loop that stays
inside free quotas and fails closed when a safety invariant is violated.

> **Secrets never live in this repository.** All credentials come from process
> environment variables supplied by GitHub Actions Secrets. `.env.example` is
> documentation and is not auto-loaded. See [docs/SECURITY.md](docs/SECURITY.md).

---

## Safety contract

The rules are code-pinned, not just documented:

| Invariant | Enforcement |
| --- | --- |
| Hard cap `$0.00` | Config validation, ledger cost entries, provider host allowlist, daily budgets |
| No paid APIs or cloud billing | Invariants reject paid hosts; offline deterministic fallback always exists |
| Human approval for payouts, payment credentials, replication, invariant changes, visibility changes | `InvariantGuard` raises unless a human approval object is supplied; bot identities are rejected |
| Secrets never written or committed | Redaction at ingress, sanitized state writes, pre-commit secret scan, CI full-tree scan |
| PII never published | Recursive sanitization before state writes and outbound inference; raw inboxes are gitignored |
| Repository stays public | Visibility assertions; public-repository runner model |
| One niche per child, max three children | Swarm niche lock, digest pinning, spawn interval |
| No auto-applied evolution | Proposals are written as artifacts; `auto_apply` is always false; protected paths are rejected |

`config/invariants.yaml` is pinned by SHA-256 in
[src/core/invariants.py](src/core/invariants.py). Any semantic change fails the
digest check until a human updates both the file and the pin in a reviewed
commit. Intrinsic checks also reject an invariants file that tries to weaken
zero-spend, approvals, redaction, or replication limits — a changed digest alone
is not sufficient.

---

## What the loop does

1. **Sensors** — ingest customer/support/GitHub/inbox feedback, sanitize PII and
   secrets, prioritize customer and support signals, match feedback to niches,
   and score niches (feedback carries the highest weight).
2. **Factory** — deterministically build a stdlib-only Python CLI product with
   tests, docs, MIT license, and reproducible packaging for each top niche.
3. **Distribution** — build a deterministic ZIP + checksums, a CSP-locked static
   landing page with no JavaScript or trackers, and local aggregate funnel
   counters. No scraping, no unsolicited outreach, no issue farming.
4. **Ledger** — append-only hash-chained JSONL records every cycle, cost,
   revenue, release, approval-gated spawn, and alert. Every cost is forced to
   `0.0`; simulated revenue is separate from human-confirmed real revenue.
5. **Allocation** — `BOOTSTRAP → VALIDATE → SCALE → MAINTAIN`. Effort shares are
   score-weighted; **money allocation is always `$0.00`**.
6. **Evolution** — analyze the ledger/feedback/heartbeat, generate deterministic
   proposals, run a verification gate, and save them for human review.
7. **Swarm** — disabled by default. When a human enables it, children require a
   committed approval, lock one niche, pin the parent invariants digest, and are
   capped at three active children.
8. **Heartbeat** — detects stale workflows/components and invalid ledger chains,
   records capped alerts, and commits sanitized state to keep schedules active.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the module map and data flow.

---

## Quick start (local, offline)

Python 3.11+ is required. The commands below use a local virtual environment.

```bash
python -m venv .venv
# Windows Git Bash:
source .venv/Scripts/activate
# macOS/Linux:
# source .venv/bin/activate

python -m pip install -e ".[dev]"

# Verify the invariant pin and print status against a temporary runtime:
python -m src.core.invariants --check
python -m src.orchestrator --status --dry-run

# Full cycle in a temporary runtime; writes nothing into the repository:
python -m src.orchestrator --cycle --dry-run

# Analysis, heartbeat, and replication checks:
python -m src.orchestrator --evolve --dry-run
python -m src.orchestrator --heartbeat --dry-run
python -m src.orchestrator --replicate --dry-run   # skipped while swarm is disabled
```

A real (non-dry-run) cycle writes sanitized state to `state/` and artifacts to
`build/` and `dist/`:

```bash
python -m src.orchestrator --cycle
python -m src.orchestrator --status
```

`--online` (or `SASK_ALLOW_NETWORK=1`) enables free-tier Groq/Gemini calls for
release notes. Without keys or permission, the run is fully deterministic and
offline. Missing keys are not errors.

`make` is optional; [Makefile](Makefile) mirrors the common commands. Run
`bash scripts/verify.sh` for the full local gate.

### Use the sample feedback

```bash
python -m src.orchestrator --cycle --dry-run \
  --feedback examples/feedback/sample-feedback.json
```

Or copy a sanitized export into `inbox/` (gitignored) for local ingestion.

---

## CLI

| Flag | Behavior |
| --- | --- |
| `--cycle` | Ingest, score, build/package/landing/release-prepare, record ledger, write state |
| `--evolve` | Analyze health and generate human-gated proposals (add `--verify-commands` to run the gate) |
| `--replicate` | Spawn an approved child replica; requires `--approval-file`; `repo` mode requires `--apply` |
| `--heartbeat` | Roll up swarm telemetry, record alerts, write heartbeat state; `--workflow <file>` records a run |
| `--status` | Print invariants digest, ledger summary, heartbeat, allocation, cycle, funnel, evolution, swarm |
| `--dry-run` | Use a temporary runtime; no repository writes |
| `--online` / `--offline` | Allow or forbid free-tier network inference |
| `--feedback PATH` | Extra JSON/JSONL feedback input (repeatable) |
| `--niche ID`, `--mode workspace\|repo`, `--approval-file PATH`, `--apply` | Replication controls |
| `--workflow FILE` | Workflow filename to timestamp for heartbeat freshness |

Exit codes: `0` success, `2` safety/policy/state error with a redacted message,
`1` unexpected error (set `SASK_DEBUG=1` to see the traceback).

---

## Configuration

- [config/invariants.yaml](config/invariants.yaml) — immutable safety invariants.
  Changing this requires a human-reviewed commit that updates the digest pin in
  [src/core/invariants.py](src/core/invariants.py) and the tests.
- [config/system.yaml](config/system.yaml) — repository owner, seed niches,
  scoring weights, provider quotas, swarm/evolution/heartbeat settings,
  compliance caps, and zero-upfront monetization placeholders.

Replace `OWNER_PLACEHOLDER` with the real GitHub owner before enabling
workflows. Keep `billing.hard_cap_usd: 0.00`, `paid_*.enabled: false`, and
`monetization.payouts.automated: false`.

### Inference fallback

`Groq → Gemini → Ollama (optional) → deterministic`. Each provider has a daily
request cap, per-request token cap, and timeout. The router caches responses,
enforces a per-cycle call cap, applies deterministic exponential backoff with
jitter, treats `429`/`5xx`/model churn as failover signals, and never logs
secrets. Daily counters persist in `state/inference_budget.json`.

---

## GitHub Actions setup

The repository must be **public** to use free standard runners. All state-writing
workflows share the concurrency group `sask-state` so the ledger is never written
concurrently.

| Workflow | Trigger | Purpose | Permissions |
| --- | --- | --- | --- |
| [revenue-cycle.yml](.github/workflows/revenue-cycle.yml) | Every 12h, manual | Build/package/release-prepare, upload artifacts, commit sanitized state | `contents: write` |
| [heartbeat-immortality.yml](.github/workflows/heartbeat-immortality.yml) | Every 6h, manual | Health checks, alerts, keepalive state commit | `contents: write` |
| [evolution-upgrade.yml](.github/workflows/evolution-upgrade.yml) | Mondays 03:00 UTC, manual | Proposals + verification gate, upload proposal artifacts | `contents: write` |
| [swarm-replicate.yml](.github/workflows/swarm-replicate.yml) | Manual only | Approval-gated child spawn | `contents: write` |
| [verify.yml](.github/workflows/verify.yml) | Push/PR, manual | Invariants, lint, types, tests, secret scan | `contents: read` |

### First-run checklist

1. Create a **public** repository named `SASK_CORE_LOOP` and push this tree.
2. Edit `config/system.yaml`: set `repo.owner` / `repo.name`; keep `$0.00`.
3. Add repository secrets (Settings → Secrets and variables → Actions):
   - `GROQ_API_KEY` — free-tier Groq key
   - `GEMINI_API_KEY` — free-tier Google AI Studio key
   - `FINE_GRAINED_GH_TOKEN` — optional, only for approved child repository creation
   No secret belongs in a file, commit, issue, log, or artifact.
4. Enable Actions for the repository. Free standard runners are used for public
   repositories; this project does not request larger runners or paid services.
5. Protect `main` (required reviews, no force pushes) and configure an
   environment named `swarm-approval` with required reviewers if you ever enable
   the swarm.
6. Run **Actions → revenue-cycle → Run workflow** once, then verify the state
   commit and artifacts. Scheduled workflows use UTC cron.
7. Set an organization/repository billing limit of `$0.00` if available, as a
   second line of defense.

### Scheduled workflow caveat

GitHub disables scheduled workflows after **60 days of repository inactivity**.
The heartbeat workflow commits sanitized state every six hours, which keeps the
repository active. This is a reliability measure, not a guarantee of unbounded
lifetime; free-runner availability and fair-use policies always apply.

### State commits

`scripts/commit_state.sh` stages only `state/` and `swarm/children/`, runs the
secret scanner, refuses non-state paths, commits as `sask-core-loop[bot]`, and
rebases/pushes. Raw inboxes, `state/inbox/`, `state/cache/`, locks, `build/`, and
`dist/` are gitignored. Release archives are uploaded as workflow artifacts (free
for public repositories), not committed to git.

---

## Zero-upfront monetization

All channels in `config/system.yaml` are placeholders and require human setup.
The loop never stores payment credentials, never automates payouts, and never
promises revenue.

Revenue ledger entries carry `simulated: true|false`:

- Simulated entries are counting exercises only.
- Real entries require a human `confirmed_by` field; bot identities are rejected.

Real revenue or payout work must be performed by a human through an approved
channel with the required human approval gate. The allocation state machine
moves to `MAINTAIN` when human-confirmed real revenue is recorded.

---

## Testing and verification

```bash
bash scripts/verify.sh
```

This runs the invariant pin check, `ruff`, strict `mypy`, `pytest`, a full-tree
secret scan, and an offline status smoke test. CI runs the same checks in
[verify.yml](.github/workflows/verify.yml). Tests use temporary directories and
never touch real credentials or external services.

---

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — modules, data flow, state, ledger
- [docs/SECURITY.md](docs/SECURITY.md) — threat model, secrets, PII, approvals
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — setup, schedules, incidents, backups
- [docs/runbooks/heartbeat.md](docs/runbooks/heartbeat.md) — stale components
- [docs/runbooks/replication.md](docs/runbooks/replication.md) — approved children
- [docs/runbooks/secrets.md](docs/runbooks/secrets.md) — rotation and incident steps
- [docs/validation-plan.md](docs/validation-plan.md) — zero-cost demand validation
- [approvals/](approvals/README.md) — human replication approvals

---

## Honest limitations

- Free tiers change. Quotas, model names, and runner rules are configurable and
  may need maintenance; the deterministic fallback keeps cycles working.
- No literal infinite compute. The system is bounded by free quotas, per-cycle
  caps, the `$0.00` budget, and GitHub fair-use policies.
- No autonomous payouts, payment credential handling, or private-data
  publication — these are human gates by design.
- Web fetching is not implemented for arbitrary sites; if added, it must respect
  `robots.txt`, rate limits, and the host allowlist in the invariants.
- Evolution never edits code automatically. A human reviews proposals, runs the
  verification gate, and merges through a reviewed commit.

## License

MIT. See [LICENSE](LICENSE).
