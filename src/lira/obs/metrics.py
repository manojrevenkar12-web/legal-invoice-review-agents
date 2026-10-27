"""Metrics as CloudWatch Embedded Metric Format: one JSON log line per event, which CloudWatch Logs turns into
metrics without an agent (Lambda and ECS awslogs). Kept in memory as well, for tests and the console.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import Any

log = logging.getLogger("lira.metrics")
NAMESPACE = "LegalInvoiceReview"
RECENT: deque[dict[str, Any]] = deque(maxlen=5000)

UNITS = {"latency_ms": "Milliseconds", "cost_usd": "None", "tokens_in": "Count", "tokens_out": "Count"}


def emit(dimensions: dict[str, str], **values: float) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [sorted(dimensions)],
                "Metrics": [{"Name": k, "Unit": UNITS.get(k, "Count")} for k in values],
            }],
        },
        **dimensions,
        **values,
    }
    RECENT.append(doc)
    log.info(json.dumps(doc))
    return doc
