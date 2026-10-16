"""Temporary canvas click capture using KLayout's per-view plugin API.

Plugin.grab_mouse gives the picker priority over native selection. The plugin
queues coordinates only; dialogs and continuation run later on the picker's timer.
See https://www.klayout.de/doc-qt5/code/class_Plugin.html#grab_mouse
"""

import pya

_factory = None


def ensure_factory():
    global _factory
    if _factory is not None:
        return _factory
    # The database-only Python package has PluginFactory but no GUI application.
    if not hasattr(pya, "MainWindow"):
        return None

    class ClickPlugin(pya.Plugin):
        def __init__(self, view):
            super().__init__()
            self.target_view, self.picker = view, None

        def accepts(self, buttons, prio):
            return bool(prio and self.picker is not None and not self.picker.finished
                        and buttons & pya.ButtonState.LeftButton)

        def mouse_click_event(self, point, buttons, prio):
            if not self.accepts(buttons, prio):
                return False
            self.picker.queue_click(point)
            return True

        def mouse_double_click_event(self, point, buttons, prio):
            return self.mouse_click_event(point, buttons, prio)

        def mouse_button_pressed_event(self, point, buttons, prio):
            # A press event denotes a drag, not a click. Do not start a native
            # selection rectangle or edit operation while choosing a reference.
            return self.accepts(buttons, prio)

        def mouse_button_released_event(self, point, buttons, prio):
            return self.accepts(buttons, prio)

    class ClickFactory(pya.PluginFactory):
        def __init__(self):
            super().__init__()
            self.plugins = []
            self.has_tool_entry = False
            self.register(100000, "shapeforge_reference_clicks", "ShapeForge Reference Clicks")

        def create_plugin(self, manager, dispatcher, view):
            plugin = ClickPlugin(view)
            self.plugins.append(plugin)
            return plugin

    _factory = ClickFactory()
    return _factory


def attach(picker):
    if not isinstance(picker.view, pya.LayoutView):
        return None  # Database-only tests use native selection with a mock view.
    factory = ensure_factory()
    if factory is None:
        return None
    factory.plugins[:] = [plugin for plugin in factory.plugins if not plugin._destroyed()]
    for plugin in factory.plugins:
        if plugin.target_view == picker.view:
            if plugin.picker is not None:
                raise RuntimeError("A reference selector is already using this view.")
            plugin.picker = picker
            try:
                plugin.grab_mouse()
            except Exception:
                plugin.picker = None
                raise
            return plugin
    raise RuntimeError("Cannot attach the reference selector to this view. Reopen the layout and retry.")


def detach(plugin):
    if plugin is not None:
        plugin.picker = None
        if not plugin._destroyed():
            plugin.ungrab_mouse()
