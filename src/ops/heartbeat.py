"""Self-healing heartbeat and stale-workflow detection."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from src.core.config import HeartbeatConfig, SwarmConfig
from src.core.errors import StateError
from src.core.io import file_lock, read_json, write_json_atomic
from src.core.paths import RepoPaths
from src.core.redact import sanitize_public_payload
from src.core.state import StateStore
from src.core.timeutil import hours_since, iso_now, parse_iso, utc_now
from src.core.types import JsonObject, JsonValue
from src.ledger.ledger import Ledger

_WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.ya?ml$")


@dataclass(frozen=True)
class HeartbeatCheck:
    stale: tuple[str, ...]
    chain_valid: bool
    last: Mapping[str, str | None]

    @property
    def healthy(self) -> bool:
        return not self.stale and self.chain_valid

    def to_json(self) -> JsonObject:
        return {
            "healthy": self.healthy,
            "stale": list(self.stale),
            "chain_valid": self.chain_valid,
            "last": dict(self.last),
        }


@dataclass(frozen=True)
class HeartbeatReport:
    checked_at: str
    workflow_file: str | None
    check: HeartbeatCheck
    alerts_recorded: int
    alerts_suppressed: int

    def to_json(self) -> JsonObject:
        return {
            "checked_at": self.checked_at,
            "workflow_file": self.workflow_file,
            "status": "ok" if self.check.healthy else "stale",
            "alerts_recorded": self.alerts_recorded,
            "alerts_suppressed": self.alerts_suppressed,
            **self.check.to_json(),
        }


class HeartbeatMonitor:
    def __init__(
        self,
        paths: RepoPaths,
        config: HeartbeatConfig,
        swarm: SwarmConfig,
        ledger: Ledger,
        *,
        dry_run: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._paths = paths
        self._config = config
        self._swarm = swarm
        self._ledger = ledger
        self._dry_run = dry_run
        self._clock = clock
        self._store = StateStore(paths.state_dir, dry_run=dry_run)
        self._workflow_path = paths.state_dir / "workflows.json"

    def run(self, workflow_file: str | None = None) -> HeartbeatReport:
        if workflow_file is not None:
            self.record_workflow_run(workflow_file)
        check = self.inspect()
        allowed = self._alerts_remaining(iso_now(self._clock()))
        recorded = 0
        suppressed = 0
        for message in check.stale:
            if recorded >= allowed:
                suppressed += 1
                continue
            self._ledger.record_alert(message, component="heartbeat")
            recorded += 1
        payload: JsonObject = {
            "status": "ok" if check.healthy else "stale",
            "workflow_file": workflow_file,
            "stale": list(check.stale),
            "chain_valid": check.chain_valid,
            "hard_cap_usd": 0.0,
        }
        self._ledger.record_heartbeat(payload)
        report = HeartbeatReport(
            checked_at=iso_now(self._clock()),
            workflow_file=workflow_file,
            check=check,
            alerts_recorded=recorded,
            alerts_suppressed=suppressed,
        )
        self._store.write_json("heartbeat", report.to_json())
        return report

    def inspect(self) -> HeartbeatCheck:
        now = self._clock()
        stale: list[str] = []
        workflow_runs = self._workflow_runs()
        for workflow in self._config.required_workflows:
            path = self._paths.root / ".github" / "workflows" / workflow.file
            if not path.is_file():
                stale.append(f"required workflow file missing: {workflow.file}")
                continue
            last_run = workflow_runs.get(workflow.file)
            if not isinstance(last_run, str):
                stale.append(f"required workflow has no recorded run: {workflow.file}")
                continue
            if hours_since(last_run, now) > workflow.max_age_hours:
                stale.append(f"required workflow is stale: {workflow.file}")
        last = {
            "cycle": self._ledger.last_timestamp("cycle"),
            "heartbeat": self._ledger.last_timestamp("heartbeat"),
            "upgrade": self._last_of("upgrade_verified", "upgrade_proposal"),
            "swarm": self._ledger.last_timestamp("spawn"),
        }
        for component, max_age in self._config.stale_after_hours.items():
            if component == "swarm" and not self._swarm.enabled:
                continue
            timestamp = last.get(component)
            if timestamp is None:
                stale.append(f"no {component} heartbeat recorded")
                continue
            if hours_since(timestamp, now) > max_age:
                stale.append(f"{component} heartbeat is stale ({max_age:g}h limit)")
        chain_valid = True
        try:
            self._ledger.validate_chain()
        except StateError:
            chain_valid = False
            stale.append("ledger hash chain is invalid")
        return HeartbeatCheck(stale=tuple(stale), chain_valid=chain_valid, last=last)

    def record_workflow_run(self, workflow_file: str) -> None:
        if not _WORKFLOW_FILE.fullmatch(workflow_file):
            raise StateError(f"unsafe workflow file name: {workflow_file!r}")
        runs = self._workflow_runs()
        runs[workflow_file] = iso_now(self._clock())
        if self._dry_run:
            return
        self._workflow_path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self._workflow_path.with_name(self._workflow_path.name + ".lock")):
            sanitized, _ = sanitize_public_payload(dict(runs))
            write_json_atomic(self._workflow_path, sanitized)

    def _workflow_runs(self) -> dict[str, JsonValue]:
        raw = read_json(self._workflow_path, {})
        if not isinstance(raw, dict):
            raise StateError("workflow run state is malformed")
        runs: dict[str, JsonValue] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str):
                runs[key] = value
        return runs

    def _last_of(self, *kinds: str) -> str | None:
        timestamps = [stamp for kind in kinds if (stamp := self._ledger.last_timestamp(kind))]
        return max(timestamps) if timestamps else None

    def _alerts_remaining(self, today: str) -> int:
        used = 0
        for entry in self._ledger.entries():
            if entry.get("kind") != "alert":
                continue
            timestamp = entry.get("ts")
            if not isinstance(timestamp, str):
                continue
            try:
                if parse_iso(timestamp).date().isoformat() == today[:10]:
                    used += 1
            except ValueError:
                continue
        return max(0, self._config.max_alerts_per_day - used)
