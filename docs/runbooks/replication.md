# Runbook: approved child replication

Replication is disabled by default and is a human decision. A child replicates
the engine for exactly one niche and pins the parent invariants digest.

## Prerequisites

1. `config/system.yaml` has `swarm.enabled: true` via a reviewed commit.
2. For repository creation, also set `swarm.allow_repo_creation: true` and pass
   `apply: true`; the default is workspace mode.
3. The `swarm-approval` GitHub Environment has required reviewers.
4. `FINE_GRAINED_GH_TOKEN` exists only if repository creation is intended, with
   the minimum scope needed to create public repositories.
5. The target niche is active, not already locked by an active child, the active
   child count is below 3, and at least 168 hours have passed since the last
   spawn (including retired children).

## Approval file

Create `approvals/<date>-<niche>.yaml` through a reviewed PR:

```yaml
action: replication
approved_by: your-human-github-handle
reference: https://github.com/OWNER/SASK_CORE_LOOP/issues/123
approved_at: 2026-12-01T00:00:00Z
```

Do not reuse `example-approval.yaml`; the workflow rejects it. Bot identities are
rejected by `Approval.__post_init__`.

## Dispatch

Actions → swarm-replicate → Run workflow:

- `mode`: `workspace` (default) or `repo`
- `niche`: leave blank for the top-scored niche or name one explicitly
- `approval_file`: the committed path under `approvals/`
- `apply`: must be `true` for `repo` mode

Local equivalent (workspace mode):

```bash
python -m src.orchestrator --replicate \
  --approval-file approvals/2026-12-01-ci-failure-diagnoser.yaml \
  --mode workspace
```

Dry run (currently returns `skipped` while swarm is disabled):

```bash
python -m src.orchestrator --replicate --dry-run \
  --approval-file approvals/example-approval.yaml
```

Wrong action, missing approval, duplicate niche, cap, interval, digest mismatch,
or disabled config all fail closed before any external action.

## What is created

`swarm/children/<child-id>/`:

- `child_policy.json` — child identity, parent digest, one niche, no overrides
- `config/child_system.yaml` — derived config; only the child niche is active,
  swarm disabled, offline default
- `config/invariants.yaml` — exact parent copy; the child must not edit it
- `state/telemetry.json` — sanitized counters for roll-up
- `README.md`, `.gitignore`

For `repo` mode, a public repository is created with the configured owner/prefix,
and the child scaffold is still registered in the niche lock and ledger.

## Roll-up

`--heartbeat` reads child telemetry, clamps malformed numeric values, computes
stale children from `last_heartbeat`/`created_at`, and writes `state/swarm.json`.
Only aggregate counts and revenue/cost totals are recorded; no child feedback is
republished.

## Retire a child

Retiring frees the niche lock but does not reset the spawn interval. Use the
lock API, then commit the sanitized registry:

```bash
python -c "from pathlib import Path; from src.swarm.niche_lock import NicheLock; \
NicheLock(Path('state/swarm_registry.json'), max_children=3).release('child-id')"
python -m src.orchestrator --status
bash scripts/commit_state.sh "retire-child"
```

Do not hand-edit the registry if it can be avoided; the model validates IDs,
statuses, and digest formats.

## Stop or roll back

- Disable `swarm.enabled` in a reviewed commit.
- Retire active children and archive their workspaces/repositories.
- Revoke the fine-grained token if it is no longer needed.
- No money, payment credentials, or paid resources are involved at any point.
