"""Order ids derived from the entry attempt.

docs/engineering/live-entry-failure-paths-design-v1.md, section 0. The entry's
clientOrderId is 32 hex characters, written on the attempt row before the entry is
sent. Every later order the attempt causes derives its id from it:

  * protective stops: ``<entry>s1``, ``<entry>s2``, ...;
  * emergency closes: ``<entry>c1``, ``<entry>c2``, ....

So the attempt row alone lets recovery find every such order on the exchange by
prefix, even when no later database write landed. The longest id stays within
Bybit's 36-character orderLinkId limit.
"""

from __future__ import annotations

import re
import uuid

ORDER_LINK_ID_MAX_LENGTH = 36
_ENTRY_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_SEQUENCE = 999


def new_entry_client_order_id() -> str:
    return uuid.uuid4().hex


def is_entry_client_order_id(value: str) -> bool:
    return bool(_ENTRY_ID.match(value))


def _derived(entry_client_order_id: str, kind: str, sequence: int) -> str:
    if not is_entry_client_order_id(entry_client_order_id):
        raise ValueError("entry clientOrderId must be 32 lowercase hex characters")
    if not 1 <= sequence <= _MAX_SEQUENCE:
        raise ValueError(f"derived order sequence must be in 1..{_MAX_SEQUENCE}")
    return f"{entry_client_order_id}{kind}{sequence}"


def stop_client_order_id(entry_client_order_id: str, sequence: int) -> str:
    return _derived(entry_client_order_id, "s", sequence)


def close_client_order_id(entry_client_order_id: str, sequence: int) -> str:
    return _derived(entry_client_order_id, "c", sequence)


def stop_prefix(entry_client_order_id: str) -> str:
    return f"{entry_client_order_id}s"


def close_prefix(entry_client_order_id: str) -> str:
    return f"{entry_client_order_id}c"
