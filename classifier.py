"""Rule-based classifier (v1). Keep the signature: classify(window) -> label.

window: list of reading dicts, oldest -> newest, ~1 per second, last ~30 s.
Each dict: drip_count, last_drip_interval_ms, time_since_last_drip_ms, weight_g
Returns: "normal" | "blockage" | "empty_bag" | "free_flow" | "unknown"

HOW IT WORKS: look at the numbers, check the rules in order, first match wins.
All thresholds are at the top. Tune them using the synthetic data / real bench data.
"""

# ---- thresholds: tune these ----
EXPECTED_INTERVAL_MS = 1200   # normal time between drops at your clamp setting
FREE_FLOW_INTERVAL_MS = 200   # drops closer than this => free-flow
FREE_FLOW_DRIPS_PER_S = 4     # avg drops per second (last 5 s) above this => free-flow
NO_DRIP_MS = 10_000           # no drop for this long => drip has stopped
SLOW_FACTOR = 2.0             # avg interval > 2x expected => partial blockage
EMPTY_WEIGHT_G = 50           # bag weight below this => empty
MIN_READINGS = 5              # need this much history before judging


def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def classify(window):
    # 1. Not enough or bad data -> say so, don't assume normal
    if len(window) < MIN_READINGS:
        return "normal"                      # still warming up
    latest = window[-1]
    if latest.get("weight_g") is None or latest["weight_g"] < 0:
        return "unknown"

    # 2. Free-flow: drops way too fast
    interval = latest.get("last_drip_interval_ms")
    recent_rate = _avg([r["drip_count"] for r in window[-5:]])
    if (interval is not None and interval < FREE_FLOW_INTERVAL_MS) or \
       (recent_rate is not None and recent_rate > FREE_FLOW_DRIPS_PER_S):
        return "free_flow"

    # 3. Drops have stopped: empty bag or blockage? Weight decides.
    if (latest.get("time_since_last_drip_ms") or 0) > NO_DRIP_MS:
        return "empty_bag" if latest["weight_g"] < EMPTY_WEIGHT_G else "blockage"

    # 4. Drops slowed way down (partial blockage)
    avg_interval = _avg([r.get("last_drip_interval_ms") for r in window])
    if avg_interval is not None and avg_interval > SLOW_FACTOR * EXPECTED_INTERVAL_MS:
        return "blockage"

    return "normal"