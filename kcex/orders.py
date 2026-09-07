"""Complete open-order reads. Request pagination is captured; response metadata is not.

Accepted envelopes below are a defensive parsing contract, not a venue guarantee.
Never use a partial set to prove that a resident stop was cancelled.
"""
from __future__ import annotations

from kcex.client import KcexClient


class IncompleteOrderList(RuntimeError):
    """The exchange response cannot establish a complete order list."""


def _integer(value, name: str) -> int:
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or value < (0 if name == "total" else 1):
        raise IncompleteOrderList(f"invalid open-order {name}")
    return value


def _page(payload) -> tuple[list, dict[str, int]]:
    metadata: dict[str, int] = {}
    data = payload
    for _ in range(4):
        if isinstance(data, list):
            return data, metadata
        if not isinstance(data, dict):
            break
        if data.get("success") is False or data.get("code") not in (None, 0, 200, "0", "200"):
            raise IncompleteOrderList("unsuccessful open-order response")
        for key in ("total", "pageSize", "pageNum"):
            if key in data:
                value = _integer(data[key], key)
                if key in metadata and metadata[key] != value:
                    raise IncompleteOrderList("conflicting open-order metadata")
                metadata[key] = value
        keys = [key for key in ("resultList", "list", "orders", "records", "rows", "data") if key in data]
        if len(keys) != 1:
            break
        data = data[keys[0]]
    raise IncompleteOrderList("unknown or ambiguous open-order envelope")


def complete_open_order_ids(client: KcexClient, *, max_pages: int = 50, page_size: int = 100) -> set[str]:
    """Drain pages, never equating a short page with completeness.

    Stable total/pageSize are honored at any accepted envelope level. Without
    total, require an explicit empty page. Repetition, metadata drift, malformed
    ids, transport errors and the bound fail closed instead of returning a subset.
    Numbered pagination is not an atomic exchange snapshot: undetectable concurrent
    membership changes still require a venue-side snapshot/cursor guarantee.
    """
    max_pages = _integer(max_pages, "max_pages")
    page_size = _integer(page_size, "pageSize")
    ids: set[str] = set()
    previous = None
    for number in range(1, max_pages + 1):
        payload = client.open_orders(page_num=number, page_size=page_size)
        rows, metadata = _page(payload)
        if "pageNum" in metadata and metadata["pageNum"] != number:
            raise IncompleteOrderList("unexpected open-order page number")
        signature = (metadata.get("total"), metadata.get("pageSize"), "pageNum" in metadata)
        if previous is not None and signature != previous:
            raise IncompleteOrderList("open-order metadata changed between pages")
        previous = signature
        if len(rows) > metadata.get("pageSize", page_size):
            raise IncompleteOrderList("open-order page exceeds declared size")
        page_size = metadata.get("pageSize", page_size)
        for row in rows:
            value = row.get("id") if isinstance(row, dict) else None
            if type(value) not in (str, int) or not str(value) or str(value).strip() != str(value):
                raise IncompleteOrderList("invalid open-order id")
            oid = str(value)
            if oid in ids:
                raise IncompleteOrderList("unstable or repeated order pagination")
            ids.add(oid)
        total = metadata.get("total")
        if total is not None:
            if len(ids) > total or (not rows and len(ids) != total):
                raise IncompleteOrderList("open-order total does not match rows")
            if len(ids) == total:
                return ids
        elif not rows:
            return ids
    raise IncompleteOrderList("open-order page limit reached; no complete list")
