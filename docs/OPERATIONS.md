# Operations

## Deployment checklist

1. Public GitHub repository `SASK_CORE_LOOP` with `main` as default branch.
2. Set `repo.owner` and `repo.name` in `config/system.yaml`; replace
   `OWNER_PLACEHOLDER` in moderation/monetization URLs where needed.
3. Add Actions secrets: `GROQ_API_KEY`, `GEMINI_API_KEY`, optional
   `FINE_GRAINED_GH_TOKEN`. Keep a `$0.00` billing limit at the org level too.
4. Protect `main`: require review, block force pushes, require `verify` checks.
5. Optional swarm: create the `swarm-approval` environment with required
   reviewers, then enable `swarm.enabled` through a reviewed commit.
6. Dispatch `revenue-cycle` once and confirm:
   - job succeeds within the timeout,
   - `state/ledger.jsonl` has a new sanitized commit,
   - `dist/**` artifacts are attached to the run,
   - `--status` reports a valid chain.
7. Dispatch `heartbeat-immortality` once to record a workflow run stamp.

## Schedules (UTC)

| Workflow | Cron | Budget | Staleness limit |
| --- | --- | --- | --- |
| `revenue-cycle` | `0 */12 * * *` | 15 min job, 180 s cycle | 26 h |
| `heartbeat-immortality` | `0 */6 * * *` | 10 min job | 8 h |
| `evolution-upgrade` | `0 3 * * 1` | 20 min job | 192 h |
| `swarm-replicate` | manual | 15 min job | 720 h when enabled |

All state writers share `concurrency.group: sask-state`, so a cycle and a
heartbeat never write the ledger at the same time. Do not add a second state
writer without using the same group.

## State commits

`scripts/commit_state.sh <label>`:

1. Configures a bot identity if none exists.
2. Stages only `state/` and `swarm/children/`.
3. Refuses any staged path outside those prefixes.
4. Exits without a commit if nothing changed.
5. Runs the secret scanner on state and child workspaces.
6. Commits, rebases with autostash, and pushes to the current branch.

Raw inboxes, caches, locks, builds, and archives are gitignored. If a commit is
rejected, do not bypass the scan; inspect the finding path and remove the source
of the leak.

## Artifacts and releases

- Cycle artifacts: `dist/**` (ZIP, `SHA256SUMS`, site) uploaded per run with
  14-day retention.
- Cycle report: `state/cycle.json`.
- Proposals: `state/proposals/**` uploaded weekly with 30-day retention.
- Release manifests: committed under `state/releases/`, one JSON per build.
- Publishing a GitHub Release is intentionally not automated in the default
  loop. Use a human-run workflow or the `ReleaseManager.publish` path after
  reviewing the manifest, archive hash, and notes.

## Quotas and offline behavior

- Groq/Gemini calls only happen when `SASK_ALLOW_NETWORK=1` (set in the cycle
  workflow only if a key exists) and at most 6 calls per cycle.
- Daily provider caps and the global network cap are persisted; when exhausted,
  the deterministic provider answers locally.
- `state/inference_budget.json` resets per UTC day.
- If a provider errors or a model is retired, failover advances to the next
  provider. Repeated failures are recorded as attempts, not fatal errors.
- To force a fully offline run, set `SASK_ALLOW_NETWORK=0` or pass `--offline`.

## Common incidents

### Heartbeat reports stale

Run `python -m src.orchestrator --status` and read `heartbeat.stale`:

- `required workflow file missing` — the workflow was renamed or deleted. Restore
  the filename or update `config/system.yaml` in a reviewed change.
- `required workflow has no recorded run` — dispatch the workflow manually; it
  records its run stamp via `--workflow`.
- `required workflow is stale` — schedules may be disabled. Re-enable the
  workflow, dispatch once, and confirm repository activity.
- `no <component> heartbeat recorded` — run the corresponding mode (`--cycle`,
  `--heartbeat`, `--evolve`). The evolution staleness window is 192 h.
- `ledger hash chain is invalid` — stop automation. Restore `state/ledger.jsonl`
  from the last known-good commit, preserve the bad file for analysis, and
  investigate how it was modified. Do not delete entries to "fix" it.

### Alerts are capped

At most `heartbeat.max_alerts_per_day` (2) alerts are written to the ledger per
UTC day; the remainder are suppressed in the report. This prevents alert floods
from growing state. Use the report's `alerts_suppressed` count for diagnosis.

### A cycle exceeds its budget

The cycle records `within_cycle_budget: false` but still completes safely. Inspect
`duration_seconds`; most time is packaging and syntax checks. Reduce the number
of products per cycle or the feedback limit only through a reviewed change.

### An accidental paid resource appears

Do not "test" it. Remove it, confirm `hard_cap_usd: 0.00`, check
`billing.paid_*: false`, and inspect the ledger for any cost entry other than
`0.0`. The ledger rejects non-zero costs; a reported non-zero cost means code or
state was altered outside the guard.

### Secrets need rotation

Follow [docs/runbooks/secrets.md](runbooks/secrets.md). Rotate first, update the
Actions Secret, then verify the next cycle uses the new value. Never print or
diff secret values.

## Backups and recovery

- State is append-only and committed, so `git log -- state/ledger.jsonl` is the
  audit trail.
- To recover, check out the last good `state/` tree, run
  `python -m src.core.invariants --check`, then `python -m src.orchestrator --status`.
- Builds and archives are reproducible from config + feedback; artifacts are
  disposable.
- Keep raw feedback local and out of git. Re-ingesting sanitized exports is safe;
  re-ingesting raw exports repeats the sanitization step.

## Disabling the loop

- Disable the scheduled workflows in the Actions UI, or pause Actions entirely.
- Optionally merge a reviewed change setting `swarm.enabled: false` (already the
  default) and keep `monetization.enabled: false`.
- Removing repository access does not revoke leaked credentials; rotate them.
