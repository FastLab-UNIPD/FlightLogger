"""Notes export to text, Markdown, LaTeX, CSV and HTML. Pure functions, no GUI."""
from __future__ import annotations

import csv
import html
import io

FORMATS = {"txt": "Plain text", "md": "Markdown (Notion, Obsidian)", "tex": "LaTeX",
           "csv": "CSV (Excel, Sheets)", "html": "HTML"}
EXTENSIONS = {k: "." + k for k in FORMATS}
FILE_FILTERS = {k: f"{v} (*.{k})" for k, v in FORMATS.items()}


def fmt_time(t: float, decimals: int = 1) -> str:
    """Seconds -> [H:]MM:SS.s (same as plots.common.fmt_time, which needs pyqtgraph)."""
    sign = "-" if t < 0 else ""
    t = abs(t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    width = 3 + decimals if decimals else 2
    sec = f"{s:0{width}.{decimals}f}"
    if sec.startswith("60"):
        s, m = 0.0, m + 1
        sec = f"{s:0{width}.{decimals}f}"
    return f"{sign}{int(h)}:{int(m):02d}:{sec}" if h else f"{sign}{int(m)}:{sec}"


def _when(n) -> str:
    if n.general:
        return "General"
    return fmt_time(n.t, 1) + ("" if n.t_end is None else f" – {fmt_time(n.t_end, 1)}")


def _items(notes, messages):
    """General notes first, then timed notes and messages merged by time."""
    notes = list(notes)
    general = [("note", n) for n in notes if n.general]
    timed = [("note", n) for n in notes if not n.general] + [("msg", m) for m in messages]
    timed.sort(key=lambda it: it[1].t)   # stable: a note stays before a message at the same time
    return general + timed


def _msg_line(m) -> str:
    return f"[{fmt_time(m.t, 1)}] [MSG {m.level}] {m.text}"


def _txt(items, title):
    lines = []
    for kind, x in items:
        if kind == "msg":
            lines.append(_msg_line(x))
            continue
        lines.append(f"[{_when(x)}] {x.category}: {x.title}")
        if x.text.strip():
            lines += ["    " + l for l in x.text.strip().splitlines()]
        lines.append("")
    return "\n".join(lines)


def _md_escape(s: str) -> str:
    return s.replace("\n", " ")


def _md(items, title):
    out = [f"# {title}", ""] if title else []
    for kind, x in items:
        if kind == "msg":
            out += [f"> ⚠ [{fmt_time(x.t, 1)}] {x.level}: {_md_escape(x.text)}", ""]
            continue
        out.append(f"### [{_when(x)}] {x.category}: {x.title}".rstrip())
        out.append("")
        if x.text.strip():
            # a blank line between paragraphs; single newlines become hard breaks
            paras = [p.strip().replace("\n", "  \n") for p in x.text.strip().split("\n\n") if p.strip()]
            for p in paras:
                out += [p, ""]
    return "\n".join(out)


_TEX = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&", "#": r"\#",
        "^": r"\textasciicircum{}", "_": r"\_", "%": r"\%", "~": r"\textasciitilde{}"}


def tex_escape(s: str) -> str:
    return "".join(_TEX.get(c, c) for c in s)


def _tex(items, title):
    out = []
    if title:
        out.append(r"\subsection*{" + tex_escape(title) + "}")
    out.append(r"\begin{itemize}")
    for kind, x in items:
        if kind == "msg":
            out.append(r"  \item \textbf{[" + fmt_time(x.t, 1) + "] " + tex_escape(x.level)
                       + ":} " + tex_escape(x.text.replace("\n", " ")))
            continue
        head = (r"  \item \textbf{[" + tex_escape(_when(x)).replace("–", "--") + "] " + tex_escape(x.category)
                + (": " + tex_escape(x.title) if x.title else "") + "}")
        if x.text.strip():
            # \newline keeps the comment's line breaks inside the item
            head += r" \newline " + r" \newline ".join(tex_escape(l) for l in x.text.strip().splitlines())
        out.append(head)
    out.append(r"\end{itemize}")
    return "\n".join(out)


def _csv(items, title):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["start", "end", "category", "title", "comment", "kind"])
    for kind, x in items:
        if kind == "msg":
            w.writerow([fmt_time(x.t, 1), "", x.level, "", x.text, "message"])
        elif x.general:
            w.writerow(["", "", x.category, x.title, x.text, "general"])
        else:
            w.writerow([fmt_time(x.t, 1), "" if x.t_end is None else fmt_time(x.t_end, 1),
                        x.category, x.title, x.text, "note"])
    return buf.getvalue()


_HTML_HEAD = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: sans-serif; max-width: 52em; margin: 2em auto; color: #222; }}
.note {{ border-left: 6px solid #555; padding: .3em .8em; margin: .8em 0; background: #fafafa; }}
.msg {{ border-left: 6px solid #b8860b; padding: .2em .8em; margin: .5em 0; color: #555; }}
.when {{ color: #666; font-family: monospace; }}
</style></head><body>
"""


def _html(items, title):
    e = html.escape
    out = [_HTML_HEAD.format(title=e(title or "Notes"))]
    if title:
        out.append(f"<h1>{e(title)}</h1>")
    for kind, x in items:
        if kind == "msg":
            out.append(f'<div class="msg"><span class="when">[{fmt_time(x.t, 1)}]</span> '
                       f"<b>{e(x.level)}</b>: {e(x.text)}</div>")
            continue
        body = f"<p>{e(x.text.strip()).replace(chr(10), '<br>')}</p>" if x.text.strip() else ""
        out.append(f'<div class="note" style="border-left-color:{e(x.color)}">'
                   f'<b><span class="when">[{e(_when(x))}]</span> {e(x.category)}'
                   f'{": " + e(x.title) if x.title else ""}</b>{body}</div>')
    out.append("</body></html>\n")
    return "\n".join(out)


_RENDERERS = {"txt": _txt, "md": _md, "tex": _tex, "csv": _csv, "html": _html}


def render(notes, fmt: str, messages=(), title: str = "") -> str:
    if fmt not in _RENDERERS:
        raise ValueError(f"unknown export format: {fmt}")
    return _RENDERERS[fmt](_items(notes, messages), title)
