"""Light Map: a Home Assistant lighting-control container.

Two planes: the switching plane in `app.core` (every bulb has a soft switch; hard switches
bind to it; a soft->bulb bridge makes the bulb follow) and behaviour modules in
`app.modules` that act only through it. `app.main` is the composition root.
"""
