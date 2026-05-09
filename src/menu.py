"""Hierarchical menu system for Tamagotchi-style navigation.

Stack-based navigation with D-pad controls. Each MenuItem can be a
leaf (has an action string) or a branch (has children submenu).
"""


class MenuItem:
    """A single menu entry — leaf or branch."""

    __slots__ = ("label", "action", "children")

    def __init__(self, label, action=None, children=None):
        """
        Args:
            label: Display text for this item.
            action: Action string returned when selected (leaf items).
            children: List of MenuItem for submenus (branch items).
        """
        self.label = label
        self.action = action
        self.children = children or []

    @property
    def is_branch(self):
        return bool(self.children)


def build_menu_tree():
    """Build the default Cortex menu tree."""
    return [
        MenuItem("Take Note", action="take_note"),
        MenuItem("Record Audio", action="record"),
        MenuItem("Notes & Recs", action="info_screen"),
        MenuItem("Games", children=[
            MenuItem("Pong", action="game_pong"),
        ]),
        MenuItem("Settings", children=[
            MenuItem("Brightness", action="adj_brightness"),
            MenuItem("Volume", action="adj_volume"),
            MenuItem("Display Hz", action="adj_hz"),
            MenuItem("WiFi Info", action="wifi_info"),
            MenuItem("BLE Info", action="ble_info"),
            MenuItem("About", action="about"),
        ]),
        MenuItem("Shutdown", action="confirm_shutdown"),
    ]


class MenuSystem:
    """Stack-based menu navigator.

    Usage:
        menu = MenuSystem(build_menu_tree())
        menu.open()
        action = menu.navigate("down")   # move cursor
        action = menu.navigate("select") # enter submenu or get action
        action = menu.navigate("back")   # go up one level
        menu.close()
    """

    def __init__(self, root_items):
        self._root = root_items
        # Stack of (items_list, cursor_index) tuples
        self._stack = []
        self._open = False

    def is_open(self):
        return self._open

    def open(self):
        """Open the menu at the root level."""
        self._stack = [(self._root, 0)]
        self._open = True

    def close(self):
        """Close the menu entirely."""
        self._stack.clear()
        self._open = False

    def navigate(self, direction):
        """Process a navigation input.

        Args:
            direction: "up", "down", "select", "back"

        Returns:
            Action string if a leaf item was selected, or None.
        """
        if not self._open or not self._stack:
            return None

        items, cursor = self._stack[-1]

        if direction == "up":
            cursor = max(0, cursor - 1)
            self._stack[-1] = (items, cursor)
            return None

        elif direction == "down":
            cursor = min(len(items) - 1, cursor + 1)
            self._stack[-1] = (items, cursor)
            return None

        elif direction == "select":
            if 0 <= cursor < len(items):
                selected = items[cursor]
                if selected.is_branch:
                    # Push submenu onto stack
                    self._stack.append((selected.children, 0))
                    return None
                else:
                    # Leaf item — return its action
                    return selected.action
            return None

        elif direction == "back":
            if len(self._stack) > 1:
                # Pop back to parent menu
                self._stack.pop()
            else:
                # At root — close menu
                self.close()
            return None

        return None

    def get_visible_items(self):
        """Get current menu items and cursor position.

        Returns:
            (items_list, cursor_index) or ([], 0) if closed.
        """
        if not self._open or not self._stack:
            return ([], 0)
        items, cursor = self._stack[-1]
        return (items, cursor)

    def get_breadcrumb(self):
        """Get breadcrumb string like 'Menu > Settings'.

        Returns:
            Breadcrumb string.
        """
        if not self._open or not self._stack:
            return ""

        parts = ["Menu"]
        # For each level beyond root, find the selected item's label
        for i in range(len(self._stack) - 1):
            items, cursor = self._stack[i]
            if 0 <= cursor < len(items):
                parts.append(items[cursor].label)

        return " > ".join(parts)

    @property
    def depth(self):
        """Current menu depth (0 = root)."""
        return max(0, len(self._stack) - 1)
