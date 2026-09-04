"""Tray menu construction (modules/tray.py).

Contract: every menu item the tray builds is accepted by pystray at
construction time. pystray validates action signatures and raises
ValueError for anything but (icon, item); one bad item kills the whole
icon, which then retries and gives up — the app keeps running with no tray
(seen 2026-09-04 after a lambda gained a defaulted parameter).
"""
from typing import List

from modules.tray import UI_POSITIONS, make_position_items


def test_position_items_build_and_dispatch() -> None:
    chosen: List[str] = []
    current = {"pos": "top-right"}
    items = make_position_items(lambda: current["pos"], chosen.append)
    assert [i.text for i in items] == [label for label, _ in UI_POSITIONS]

    checked = [i.checked for i in items]
    assert checked == [pos == "top-right" for _, pos in UI_POSITIONS]

    items[3](None)  # pystray invokes items with the icon; item is bound internally
    assert chosen == ["bottom-left"]
