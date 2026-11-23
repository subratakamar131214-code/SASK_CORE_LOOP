# Architecture

SASK_CORE_LOOP is a single-process Python 3.11+ orchestrator wrapped by a small
set of independent modules. Every external effect is gated: network access is
opt-in, writes are atomic and sanitized, paid spending is impossible, and
replication/evolution require human review.

```
                 ┌──────────────────── config/invariants.yaml (digest-pinned)
                 │
feedback/inbox ──┴─► sensors ──► scoring ──► allocation ──► factory ──► distribution
                     │                                      (build)      (zip/site)
                     │                                                      │
                     └──────────────► ledger ◄──────── release manifest ◄──┘
                                       │
 evolution (analyze → propose → gate) ◄┘
 heartbeat (stale checks → alerts)   ◄─┘
 swarm (approval → niche lock → child workspace → rollup) ◄─┘
```

## Module map

| Package | Responsibility |
| --- | --- |
| `src/core` | Invariant guard, typed config, canonical JSON/atomic IO, locks, time, redaction, sanitized state store |
| `src/sensors` | Feedback ingestion and prioritization, niche scoring, opportunity discovery, PII sanitization |
| `src/inference` | Provider failover (Groq → Gemini → Ollama → deterministic), budget, cache, backoff, batching types |
| `src/ledger` | Append-only hash chain, $0.00 enforcement, allocation state machine |
| `src/factory` | Product specs, deterministic templates, tests/docs generation, reproducible build |
| `src/distribution` | License-checked deterministic ZIP, static landing page, privacy-preserving funnel counters |
| `src/compliance` | Policy engine (host allowlist, robots, licenses, anti-abuse), fail-closed secret scanner |
| `src/ops` | GitHub CLI wrapper, heartbeat monitor, release manifests |
| `src/swarm` | Child model, niche lock, approval-gated spawner, telemetry roll-up |
| `src/evolution` | Observation analyzer, deterministic proposer, human-gated verification gate |
| `src/orchestrator.py` | CLI modes, context assembly, state persistence, status output |

## Data flow: a revenue cycle

1. `build_context` loads and validates both YAML configs. Invariant load fails if
   the digest pin or an intrinsic safety check fails. A dry run creates a
   temporary runtime root; otherwise the repository root is the runtime.
2. Feedback arrives from `--feedback` files, `state/inbox/`, and root `inbox/`
   (raw inputs are gitignored). The ingestor redacts secrets, sanitizes PII,
   deduplicates, scores priority, and matches niches by keyword.
3. `NicheScorer` applies weights from `config/system.yaml`; feedback is the
   largest component and must remain the largest (validated at load).
4. `AllocationStateMachine` chooses the next state and effort shares. Monetary
   allocation is hard-coded to `$0.00`.
5. For at most two top niches, `ProductBuilder` renders a deterministic
   stdlib-only Python CLI, tests, docs, and MIT license into `build/<niche>/`.
   Python files are syntax-compiled before the build is accepted.
6. `PackageBuilder` checks the license allowlist, scans for secrets, refuses
   symlinks, and writes a deterministic ZIP plus `SHA256SUMS` under
   `dist/<niche>/`.
7. `LandingPageBuilder` writes a CSP-locked static page with no JavaScript,
   trackers, or forms under `dist/site/<niche>/`.
8. `ReleaseManager` writes a release manifest under `state/releases/` with the
   build digest, archive hash, tag, and sanitized notes. Publishing to GitHub is
   a separate human-triggered action.
9. The ledger records a `cost` entry forced to `0.0`, then a `cycle` entry with
   verification, duration, scores, and hashes. The chain is validated and state
   JSON is written through the sanitizing `StateStore`.

## Inference failover

`InferenceRouter` receives sanitized messages and a task key. It checks the
response cache, then tries enabled providers in order. Each attempt records an
outcome without credentials. Conditions such as `429`, timeouts, model-not-found
churn, and empty responses trigger the next provider. Backoff is deterministic
and bounded. When offline, no network provider is constructed and the
deterministic provider answers from local templates. The persisted budget file
caps daily requests across runs; a per-cycle call cap prevents runaway loops.

Allowed provider hosts are enforced twice: by config/invariants and by the
policy engine before any request. Paid AI hosts are rejected outright.

## State layout

Runtime paths come from `RepoPaths`:

| Path | Contents | Committed |
| --- | --- | --- |
| `state/ledger.jsonl` | Hash-chained append-only ledger | Yes (sanitized) |
| `state/feedback.json`, `scores.json`, `opportunities.json` | Sanitized sensor outputs | Yes |
| `state/allocation.json`, `cycle.json`, `funnel.json` | Allocation and cycle summaries | Yes |
| `state/releases/*.json` | Release manifests | Yes |
| `state/proposals/*.json` | Evolution proposals | Yes |
| `state/heartbeat.json`, `workflows.json` | Health and workflow run stamps | Yes |
| `state/swarm.json`, `swarm_registry.json` | Swarm roll-up and niche lock | Yes |
| `state/inference_budget.json` | Daily request counters | Yes |
| `state/cache/` | Response cache | No (gitignored) |
| `state/inbox/`, `inbox/` | Raw feedback | No (gitignored) |
| `build/`, `dist/` | Builds and artifacts | No (workflow artifacts) |
| `swarm/children/<id>/` | Child scaffold, policy, telemetry | Yes (sanitized) |

All JSON writes go through `StateStore`, which recursively sanitizes payloads.
Lock files are created atomically and stale locks expire; they are gitignored.

## Ledger

Each entry contains `schema_version`, `seq`, `ts`, `kind`, `actor`, `payload`,
`prev_hash`, and `hash`, where `hash` is SHA-256 over canonical JSON of the
entry without its own hash. Kinds include `init`, `cost`, `revenue`,
`allocation`, `cycle`, `release`, `heartbeat`, `spawn`, `spawn_rollup`,
`upgrade_proposal`, `upgrade_verified`, `alert`, and `compliance`.

Validation recomputes every hash, checks sequence and `prev_hash`, scans
payloads for secrets, and rejects any non-zero cost. Real revenue requires
`simulated: false` plus a human `confirmed_by`; bot identities are refused.

## Allocation

- `BOOTSTRAP` — first cycles; effort split by niche score; no money.
- `VALIDATE` — after a verified cycle; requires three verified cycles and one
  conversion proxy to advance.
- `SCALE` — free-tier effort scaling; returns to `VALIDATE` on failure.
- `MAINTAIN` — only after human-confirmed real revenue; still no automated spend.

## Trust boundaries

- **Untrusted input**: feedback text, labels, IDs, filenames, and any future web
  content. All are sanitized and size-limited.
- **Untrusted output**: model completions. They are sanitized before state or
  release notes and never executed.
- **Trusted code**: `src/`, `config/invariants.yaml` (digest-pinned), workflows.
  These are protected paths for evolution and child replicas.
- **External actions**: GitHub repo/release creation and any publication are
  human-triggered and approval-gated.

## Determinism

Generated products, scores, opportunities, proposals, and offline completions
are deterministic given the same config and feedback. Packaging pins ZIP entry
times and file ordering, and SHA-256 digests are recorded for builds, archives,
pages, and ledger entries. This makes dry runs reproducible and diffable.
