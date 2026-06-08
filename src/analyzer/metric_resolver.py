"""Resolve metric pattern wildcards to actual EARS_METRIC instance names.

In v2 the candidate instance names come from a distinct-``EARS_METRIC`` terms
aggregation (see :meth:`src.es.client.ESClient.get_metric_names`), not from the
index mapping — every metric is the same ``EARS_VALUE`` column distinguished by
its ``EARS_METRIC`` value. The fnmatch core is unchanged from v1.
"""
from __future__ import annotations

import fnmatch


def resolve_metric_patterns(
    patterns: list[str], available_metrics: list[str]
) -> dict[str, list[str]]:
    """For each pattern, return the ``available_metrics`` it matches via fnmatch.

    Returns ``{pattern: [matched, ...]}``; a non-matching pattern maps to ``[]``.
    A literal (wildcard-free) pattern matches only its exact name.
    """
    return {
        pattern: [m for m in available_metrics if fnmatch.fnmatch(m, pattern)]
        for pattern in patterns
    }
