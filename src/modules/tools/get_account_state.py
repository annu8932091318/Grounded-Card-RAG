"""Supplied tool: get_account_state(customer_id) -> dict. Contract is fixed (data/tools.md).
Simulates 400 ms latency and fails with UpstreamTimeout ~1 call in 12 (TOOL_FAIL_RATE)."""
import json
import random
import time

from src.const.config import config
from src.modules.tools.errors import CustomerNotFound, UpstreamTimeout

LATENCY_S = 0.4
_rng = random.Random(config.tool_fail_seed) if config.tool_fail_seed else random.Random()


def get_account_state(customer_id: str) -> dict:
    time.sleep(LATENCY_S)
    if _rng.random() < config.tool_fail_rate:  # WHY read config each call: eval/demo override the rate per case
        raise UpstreamTimeout("account service did not respond")
    accounts = json.loads((config.data_dir / "accounts.json").read_text(encoding="utf-8"))
    if customer_id not in accounts:
        raise CustomerNotFound(customer_id)
    return dict(accounts[customer_id])
