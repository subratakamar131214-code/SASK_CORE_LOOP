# Runbook: heartbeat and stale components

The heartbeat runs every six hours and records a run stamp, checks required
workflow files and freshness, inspects component timestamps in the ledger, and
verifies the hash chain. At most two alerts per UTC day are written; the rest are
reported as suppressed.

## Manual check

```bash
python -m src.orchestrator --status
python -m src.orchestrator --heartbeat --workflow heartbeat-immortality.yml
```

`--heartbeat` appends a heartbeat ledger entry, writes `state/heartbeat.json`,
and records the workflow stamp in `state/workflows.json`. CI commits the result.

## What "healthy" means

- Both required workflow files exist under `.github/workflows/`.
- Each required workflow has a recorded run within its `max_age_hours`.
- Cycle, heartbeat, and upgrade entries are within their stale thresholds
  (`26 h`, `8 h`, `192 h`); swarm is checked only when enabled (`720 h`).
- Every ledger hash, sequence, and `prev_hash` is valid.
- No non-zero cost entry exists.

## Recovery steps by message

| Message | Action |
| --- | --- |
| `required workflow file missing: <file>` | Restore the file or update `config/system.yaml` in a reviewed PR. |
| `required workflow has no recorded run: <file>` | Dispatch the workflow manually once; CI records the stamp. |
| `required workflow is stale: <file>` | Check Actions is enabled. Re-enable the workflow, dispatch it, and confirm the state commit. |
| `no cycle heartbeat recorded` | Run or dispatch `revenue-cycle`. |
| `no heartbeat heartbeat recorded` | Run or dispatch `heartbeat-immortality`. |
| `no upgrade heartbeat recorded` | Run or dispatch `evolution-upgrade`. |
| `no swarm heartbeat recorded` but swarm enabled | Dispatch an approved replication or run a swarm roll-up via `--heartbeat`. |
| `ledger hash chain is invalid` | Stop automation. Preserve the bad ledger, restore the last known-good commit, and investigate. |
| `<component> heartbeat is stale` | Dispatch the component or investigate the failure in the Actions log. |

## Chain failure

A hash-chain failure is never self-healed by appending. Preserve evidence:

```bash
cp state/ledger.jsonl /tmp/ledger.bad.jsonl
git log --oneline -- state/ledger.jsonl
git checkout <last-good-commit> -- state/ledger.jsonl
python -m src.orchestrator --status
```

Then determine whether the file was edited outside the orchestrator, a disk
error occurred, or two writers ran concurrently. The state workflows share the
`sask-state` concurrency group; a new writer must join it.

## Keepalive caveat

GitHub disables scheduled workflows after 60 days of repository inactivity. The
heartbeat's state commit keeps the repository active, but this is not a promise of
unlimited uptime. If Actions are paused, workflows must be re-enabled manually.

## Schedule changes

Cron lives in the workflow files, which are protected. Change it in a reviewed
PR and update `heartbeat.stale_after_hours` / `required_workflows` together so
the thresholds remain realistic.
