"""Qt-free model of the notes / messages overlay drawn over the video.

The live widget and the video export share this code, so the exported video shows exactly what
the viewer shows: which boxes exist at a time ``t`` (``overlay_items``), where they go on screen
(``place_boxes``, ``admit``) and how they look (``box_style``).
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape

OV_MAX_BOXES = 6


@dataclass
class OverlayOptions:
    notes: bool = True
    messages: bool = True
    note_lead: float = 1.5     # a note appears this much before its time
    note_hold: float = 4.0     # ... and stays this long after its time (or the end of a span)
    msg_lead: float = 1.0
    msg_hold: float = 6.0
    flash_in: float = 0.4      # background flash: fades in before / out after the timestamp
    flash_out: float = 0.7


def _pulse(dt: float, opts: OverlayOptions) -> float:   # dt = now - event time
    if -opts.flash_in <= dt <= 0:
        return 1 + dt / opts.flash_in
    if 0 < dt <= opts.flash_out:
        return 1 - dt / opts.flash_out
    return 0.0


def overlay_items(t: float, notes, messages, opts: OverlayOptions) -> list[tuple[str, float, object]]:
    """Boxes shown at log time ``t``: (html, flash 0..1, key) each. ``notes`` / ``messages`` are
    iterables of Note / LogMessage objects."""
    rows: list[tuple[str, float, object]] = []

    def span_pulse(start: float, end: float) -> float:   # lit for the whole span
        if t < start:
            return _pulse(t - start, opts)
        if t <= end:
            return 1.0
        return _pulse(t - end, opts)

    if opts.notes:
        for n in notes:
            if n.general:
                continue
            if n.t_end is None:
                shown_until = n.t + opts.note_hold
                flash = _pulse(t - n.t, opts)
            else:
                # a span is on screen from before its start until after its end, with the
                # same offsets as an instant note, and flashes for its whole length
                shown_until = n.t_end + opts.note_hold
                flash = span_pulse(n.t, n.t_end)
            if n.t - opts.note_lead <= t <= shown_until:
                # the whole comment, one overlay line per line of the note
                body = (" – " + "<br>".join(escape(l) for l in n.text.strip().splitlines())
                        if n.text.strip() else "")
                rows.append((f'<span style="color:{n.color}">■</span> <b>{escape(n.title)}</b>{body}',
                             flash, ("note", n.id)))
    if opts.messages:
        colors = {"error": "#ff7b72", "warning": "#f0c04c"}
        for m in messages:
            if t - opts.msg_hold <= m.t <= t + opts.msg_lead:
                c = colors.get(m.level.lower(), "#cccccc")
                rows.append((f'<span style="color:{c}">{escape(m.level)}</span> {escape(m.text)}',
                             _pulse(t - m.t, opts), ("message", m.t, m.text)))
    return rows


def flash_level(flash: float) -> int:
    """Flash 0..1 quantized to the 9 background levels (0..8) used for drawing."""
    return round(max(0.0, min(1.0, flash)) * 8)


def admit(prev_keys, wanted_keys, max_boxes: int = OV_MAX_BOXES) -> list:
    """Keys that get a box: those already shown that are still wanted keep theirs; newcomers
    fill the free slots in order and the rest wait until a slot is free."""
    wanted = list(dict.fromkeys(wanted_keys))
    wset = set(wanted)
    kept = [k for k in prev_keys if k in wset]
    kset = set(kept)
    for k in wanted:
        if len(kept) >= max_boxes:
            break
        if k not in kset:
            kept.append(k)
            kset.add(k)
    return kept


def place_boxes(order_prev: dict, heights: dict, gap: int) -> dict:
    """Top of each box. ``order_prev``: key -> previous y (None = not placed yet; dict order
    breaks ties and orders the new ones). Boxes already placed keep their y unless they now
    overlap (pushed below); new ones go to the first free space from the top."""
    keys = list(order_prev)
    order = sorted((k for k in keys if order_prev[k] is not None), key=lambda k: order_prev[k])
    order += [k for k in keys if order_prev[k] is None]
    ys: dict = {}
    placed: list = []
    for key in sorted(order, key=lambda k: order_prev[k] is None):
        y = order_prev[key] if order_prev[key] is not None else 0
        h = heights[key]
        for other in sorted(placed, key=lambda k: ys[k]):
            if y < ys[other] + heights[other] + gap and ys[other] < y + h + gap:
                y = ys[other] + heights[other] + gap   # taken (or grown into): next free spot
        ys[key] = y
        placed.append(key)
    return {k: ys[k] for k in keys}


def box_style(level: int, font_px: int) -> str:
    """Stylesheet of one box: background brightness (flash level 0..8), proportional text/padding."""
    k = level / 8
    r, g, b, a = (round(40 * k), round(40 * k), round(55 * k), round(150 + 35 * k))
    fp = font_px
    return (f"background: rgba({r}, {g}, {b}, {a}); color: white; "
            f"border-radius: {round(fp * 0.46)}px; "
            f"padding: {round(fp * 0.46)}px {round(fp * 0.6)}px; font-size: {fp}px;")
