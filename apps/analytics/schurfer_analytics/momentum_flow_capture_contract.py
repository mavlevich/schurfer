"""Shared identity of the collector-written Bybit momentum dataset.

The capture version must match ``apps/collector/internal/momentumcapture/writer.go``.
Keeping the Python readers on this one definition avoids drift between reports and
prospective workers while preserving the explicit cross-language contract boundary.
"""

BYBIT_MOMENTUM_CAPTURE_VERSION = "v1"
BYBIT_MOMENTUM_EXCHANGE = "bybit"
BYBIT_MOMENTUM_MARKET_TYPE = "linear"
