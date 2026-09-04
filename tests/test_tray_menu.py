"""Tray menu model (modules/tray.py).

Contract: the menu is built as a plain MenuItem tree and rendered into Qt
on every open. Building the tree must not need a display, and every item
must carry a zero-argument action (a signature mismatch used to kill the
whole tray under pystray; under Qt it would silently do nothing).
"""
from typing import List

from modules.tray import UI_POSITIONS, MenuItem, SEPARATOR, make_position_items


def test_position_items_build_and_dispatch() -> None:
    chosen: List[str] = []
    items = make_position_items(lambda: "top-right", chosen.append)
    assert [i.label for i in items] == [label for label, _ in UI_POSITIONS]
    assert [i.checked for i in items] == [pos == "top-right" for _, pos in UI_POSITIONS]

    items[3].action()
    assert chosen == ["bottom-left"]


def test_menu_item_defaults() -> None:
    item = MenuItem("Plain")
    assert item.checked is None and item.enabled and item.children is None
    assert SEPARATOR.separator
