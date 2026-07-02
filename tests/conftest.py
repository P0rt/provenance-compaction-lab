from __future__ import annotations

import numpy as np
import pytest

from provlab.llm import MockProseChannel
from provlab.policies import Policy, default_policies
from provlab.replay import ReplayConfig, ReplayResult, run_replay
from provlab.trajectory import Profile

MED = Profile(p_unverified=0.15, p_fallback=0.15, p_flaky=0.08, p_stale=0.25)


def make_channel(seed: int) -> MockProseChannel:
    return MockProseChannel(rng=np.random.default_rng(seed + 1_000_003))


def run_once(
    seed: int,
    steps: int = 300,
    cadence: int = 10,
    keep_hops: int = 5,
    penalty: float = 0.02,
    rehydrate: bool = True,
) -> ReplayResult:
    config = ReplayConfig(
        seed=seed,
        steps=steps,
        decision_every=5,
        compaction_cadence=cadence,
        keep_hops=keep_hops,
        reconstruction_penalty=penalty,
        profile=MED,
        rehydrate=rehydrate,
        hop_log_path=None,
    )
    return run_replay(config, default_policies(allowlist_window=8), make_channel(seed))


@pytest.fixture(scope="session")
def policies() -> tuple[Policy, ...]:
    return default_policies(allowlist_window=8)
