"""Approval-gated swarm replication with one niche per child."""

from src.swarm.aggregator import ChildTelemetry, SwarmAggregator, SwarmRollup
from src.swarm.models import CHILD_MODES, CHILD_STATUSES, ChildReplica
from src.swarm.niche_lock import NicheLock
from src.swarm.spawner import SpawnResult, SwarmSpawner, load_approval

__all__ = [
    "CHILD_MODES",
    "CHILD_STATUSES",
    "ChildReplica",
    "ChildTelemetry",
    "NicheLock",
    "SpawnResult",
    "SwarmAggregator",
    "SwarmRollup",
    "SwarmSpawner",
    "load_approval",
]
