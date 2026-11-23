"""Append-only, hash-chained ledger that enforces the $0.00 hard cap."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from src.core.errors import ApprovalRequired, InvariantViolation, StateError
from src.core.invariants import InvariantGuard
from src.core.io import append_jsonl, canonical_json, file_lock, read_jsonl, sha256_hex
from src.core.redact import sanitize_public_payload
from src.core.timeutil import iso_now, utc_now
from src.core.types import JsonObject, JsonValue

SCHEMA_VERSION = 1
GENESIS_HASH = "GENESIS"
LEDGER_KINDS = frozenset(
    {
        "init",
        "cost",
        "revenue",
        "allocation",
        "cycle",
        "release",
        "heartbeat",
        "spawn",
        "spawn_rollup",
        "upgrade_proposal",
        "upgrade_verified",
        "alert",
        "compliance",
    }
)


@dataclass(frozen=True)
class LedgerSummary:
    entries: int
    real_revenue_usd: float
    simulated_revenue_usd: float
    cost_usd: float
    counts: Mapping[str, int]
    last_ts: str | None
    chain_valid: bool

    def to_json(self) -> JsonObject:
        return {
            "entries": self.entries,
            "real_revenue_usd": round(self.real_revenue_usd, 6),
            "simulated_revenue_usd": round(self.simulated_revenue_usd, 6),
            "cost_usd": round(self.cost_usd, 6),
            "counts": dict(sorted(self.counts.items())),
            "last_ts": self.last_ts,
            "chain_valid": self.chain_valid,
        }


def _is_bot(name: str) -> bool:
    lowered = name.strip().lower()
    return any(
        marker in lowered
        for marker in ("[bot]", "github-actions", "dependabot", "renovate-bot", "actions-bot")
    )


class Ledger:
    def __init__(
        self,
        path: Path,
        guard: InvariantGuard,
        *,
        actor: str = "sask-orchestrator",
        dry_run: bool = False,
    ) -> None:
        self._path = path
        self._guard = guard
        self._actor = actor
        self._dry_run = dry_run
        self._pending: list[JsonObject] = []

    @property
    def path(self) -> Path:
        return self._path

    def _read_entries(self) -> list[JsonObject]:
        raw_entries = read_jsonl(self._path)
        entries: list[JsonObject] = []
        for index, raw in enumerate(raw_entries):
            if not isinstance(raw, dict):
                raise StateError(f"ledger entry {index} is not an object")
            entries.append(raw)
        return entries

    def entries(self) -> list[JsonObject]:
        return [*self._read_entries(), *self._pending]

    def validate_chain(self) -> None:
        previous_hash = GENESIS_HASH
        entries = self.entries()
        for index, entry in enumerate(entries, start=1):
            if entry.get("schema_version") != SCHEMA_VERSION:
                raise StateError(f"ledger entry {index} has unsupported schema")
            if entry.get("seq") != index:
                raise StateError(f"ledger sequence gap at entry {index}")
            if entry.get("prev_hash") != previous_hash:
                raise StateError(f"ledger hash chain broken at entry {index}")
            stored_hash = entry.get("hash")
            if not isinstance(stored_hash, str):
                raise StateError(f"ledger entry {index} has no hash")
            body = {key: value for key, value in entry.items() if key != "hash"}
            expected_hash = sha256_hex(canonical_json(cast(JsonValue, body)))
            if stored_hash != expected_hash:
                raise StateError(f"ledger entry {index} hash mismatch (tampering detected)")
            kind = entry.get("kind")
            payload = entry.get("payload")
            if kind == "cost" and isinstance(payload, dict):
                amount = payload.get("amount_usd")
                if not isinstance(amount, (int, float)) or float(amount) != 0.0:
                    raise InvariantViolation("ledger contains a non-zero cost entry")
            previous_hash = stored_hash

    def _last_entry(self) -> JsonObject | None:
        entries = self.entries()
        return entries[-1] if entries else None

    def append(
        self,
        kind: str,
        payload: Mapping[str, JsonValue],
        *,
        actor: str | None = None,
    ) -> JsonObject:
        if kind not in LEDGER_KINDS:
            raise StateError(f"unsupported ledger kind: {kind!r}")
        sanitized_payload, _ = sanitize_public_payload(dict(payload))
        if not isinstance(sanitized_payload, dict):
            raise StateError("ledger payload must be an object after sanitization")
        self._validate_payload(kind, sanitized_payload)
        if self._dry_run:
            last = self._last_entry()
            seq = int(cast(int, last["seq"])) + 1 if last else 1
            prev_hash = cast(str, last["hash"]) if last else GENESIS_HASH
            entry = self._build_entry(seq, prev_hash, kind, sanitized_payload, actor)
            self._pending.append(entry)
            return entry

        with file_lock(self._path.with_name(self._path.name + ".lock")):
            last = self._last_entry()
            seq = int(cast(int, last["seq"])) + 1 if last else 1
            prev_hash = cast(str, last["hash"]) if last else GENESIS_HASH
            entry = self._build_entry(seq, prev_hash, kind, sanitized_payload, actor)
            append_jsonl(self._path, entry)
        return entry

    def _build_entry(
        self,
        seq: int,
        prev_hash: str,
        kind: str,
        payload: dict[str, JsonValue],
        actor: str | None,
    ) -> JsonObject:
        entry: JsonObject = {
            "schema_version": SCHEMA_VERSION,
            "seq": seq,
            "ts": iso_now(),
            "kind": kind,
            "actor": actor or self._actor,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        entry["hash"] = sha256_hex(canonical_json(cast(JsonValue, entry)))
        return entry

    def _validate_payload(self, kind: str, payload: dict[str, JsonValue]) -> None:
        if kind == "cost":
            amount = payload.get("amount_usd")
            if not isinstance(amount, (int, float)) or isinstance(amount, bool):
                raise StateError("cost entries require numeric amount_usd")
            self._guard.assert_zero_spend(float(amount), context="ledger cost")
        elif kind == "revenue":
            amount = payload.get("amount_usd")
            if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
                raise StateError("revenue entries require non-negative amount_usd")
            simulated = payload.get("simulated")
            if not isinstance(simulated, bool):
                raise StateError("revenue entries must declare simulated: true|false")
            if not simulated:
                confirmed_by = payload.get("confirmed_by")
                if not isinstance(confirmed_by, str) or not confirmed_by.strip():
                    raise ApprovalRequired(
                        "real revenue entries require a human confirmed_by field"
                    )
                if _is_bot(confirmed_by):
                    raise ApprovalRequired("bots cannot confirm real revenue")
        elif kind == "spawn":
            if not isinstance(payload.get("niche_id"), str):
                raise StateError("spawn entries require niche_id")
            child_digest = payload.get("child_invariants_digest")
            if not isinstance(child_digest, str):
                raise StateError("spawn entries require child_invariants_digest")
            self._guard.assert_child_invariants(child_digest)

    def ensure_genesis(self, invariants_digest: str) -> None:
        if self.entries():
            return
        self.append(
            "init",
            {
                "system": "SASK_CORE_LOOP",
                "invariants_digest": invariants_digest,
                "hard_cap_usd": 0.0,
                "note": "zero-billing ledger initialized",
            },
        )

    def record_cost(self, amount_usd: float, reason: str) -> JsonObject:
        self._guard.assert_zero_spend(amount_usd, context="record_cost")
        return self.append("cost", {"amount_usd": float(amount_usd), "reason": reason})

    def record_revenue(
        self,
        amount_usd: float,
        *,
        source: str,
        simulated: bool,
        evidence: str,
        confirmed_by: str | None = None,
    ) -> JsonObject:
        return self.append(
            "revenue",
            {
                "amount_usd": float(amount_usd),
                "currency": "USD",
                "source": source,
                "simulated": simulated,
                "evidence": evidence,
                "confirmed_by": confirmed_by,
            },
        )

    def record_heartbeat(self, payload: Mapping[str, JsonValue]) -> JsonObject:
        return self.append("heartbeat", payload)

    def record_cycle(self, payload: Mapping[str, JsonValue]) -> JsonObject:
        return self.append("cycle", payload)

    def record_alert(self, message: str, *, component: str) -> JsonObject:
        return self.append("alert", {"message": message, "component": component})

    def last_timestamp(self, kind: str | None = None) -> str | None:
        for entry in reversed(self.entries()):
            if kind is None or entry.get("kind") == kind:
                timestamp = entry.get("ts")
                return timestamp if isinstance(timestamp, str) else None
        return None

    def summary(self, *, strict: bool = False) -> LedgerSummary:
        chain_valid = True
        try:
            self.validate_chain()
        except (StateError, InvariantViolation):
            if strict:
                raise
            chain_valid = False
        entries = self.entries()
        real_revenue = 0.0
        simulated_revenue = 0.0
        cost = 0.0
        counts: dict[str, int] = {}
        last_ts: str | None = None
        for entry in entries:
            kind = str(entry.get("kind"))
            counts[kind] = counts.get(kind, 0) + 1
            timestamp = entry.get("ts")
            if isinstance(timestamp, str):
                last_ts = timestamp
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            amount = payload.get("amount_usd")
            if not isinstance(amount, (int, float)):
                continue
            if kind == "cost":
                cost += float(amount)
            elif kind == "revenue":
                if payload.get("simulated") is True:
                    simulated_revenue += float(amount)
                else:
                    real_revenue += float(amount)
        return LedgerSummary(
            entries=len(entries),
            real_revenue_usd=real_revenue,
            simulated_revenue_usd=simulated_revenue,
            cost_usd=cost,
            counts=counts,
            last_ts=last_ts,
            chain_valid=chain_valid,
        )


def init_ledger(path: Path, guard: InvariantGuard, *, dry_run: bool = False) -> Ledger:
    ledger = Ledger(path, guard, dry_run=dry_run)
    ledger.ensure_genesis(guard.digest)
    return ledger


def now_iso() -> str:
    return iso_now(utc_now())
