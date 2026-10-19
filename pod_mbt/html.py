"""The report's HTML shell and the few table helpers every section uses.

Separate from `analyze.py` for the same reason `figure.py` is: what a table looks like is not
what a number means, and mixing them makes both harder to change. The palette and the three type
roles are the Blueprint's, so a report and the design it came from read as one thing rather than
two documents about the same device.

Sections are numbered. A test report is referred to — "see section 6" in a bug, in a review — and
a numbered section is the only kind you can point at. The numbers are assigned at assembly, not
written into each section, because a crash section appears only when there was one and everything
below it shifts down.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

DASH = "&mdash;"


def esc(x: Any) -> str:
    return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def num(x: Any, unit: str = "") -> str:
    """A figure, group-separated, in the monospace role. Blank stays blank rather than zero."""
    if x is None or x == "":
        return f'<span class="dash">{DASH}</span>'
    body = f"{x:,}" if isinstance(x, int) else (f"{x:,.1f}" if isinstance(x, float) else esc(x))
    return f'<span class="n">{body}{("&thinsp;" + esc(unit)) if unit else ""}</span>'


def pct(a: Optional[int], b: Optional[int]) -> str:
    if not b or a is None:
        return f'<span class="dash">{DASH}</span>'
    return f'<span class="n">{a / b:.1%}</span>'


def delta(cur: Optional[float], base: Optional[float], unit: str = "") -> str:
    """This run against a baseline, as a signed difference.

    A difference, not both figures side by side: the current value is already in the row, and a
    reader comparing two columns of numbers does the subtraction in their head and sometimes gets
    it wrong. `new` means the row had no counterpart in the baseline at all, which is a different
    thing from no change and is the case that usually explains the rest of the table.
    """
    if base is None:
        return '<span class="d new">new</span>'
    if cur is None:
        return f'<span class="dash">{DASH}</span>'
    d = cur - base
    if abs(d) < 1e-9:
        return '<span class="d flat">0</span>'
    body = f"{d:+,}" if isinstance(d, int) or float(d).is_integer() else f"{d:+,.1f}"
    return f'<span class="d {"up" if d > 0 else "down"}">{body}{esc(unit)}</span>'


def code(x: Any) -> str:
    return f"<code>{esc(x)}</code>"


def table(headers: Sequence[str], rows: Iterable[Sequence[str]],
          right: Sequence[int] = (), foot: Optional[Sequence[str]] = None,
          note: str = "", title: str = "") -> str:
    """One table. `right` lists the column indices that hold figures and align right.

    `title` names it. A section can hold more than one table and they answer different questions —
    what a test case declares is not how much of it a run reached — so each is named rather than
    left for a reader to infer from its columns.

    Cells arrive as HTML, already escaped by their caller — the helpers above do that, and a
    section that builds a cell by hand is the one place to look when something renders as markup.
    """
    def cls(i: int) -> str:
        return ' class="r"' if i in right else ""
    head = "".join(f"<th{cls(i)}>{h}</th>" for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f"<td{cls(i)}>{c}</td>" for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    tfoot = ("<tfoot><tr>" + "".join(f"<td{cls(i)}>{c}</td>" for i, c in enumerate(foot))
             + "</tr></tfoot>") if foot else ""
    cap = f"<figcaption>{note}</figcaption>" if note else ""
    ttl = f'<h3 class="tname">{esc(title)}</h3>' if title else ""
    return (f'{ttl}<div class="tw"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody>{tfoot}</table></div>{cap}")


def section(n: int, title: str, body: str, sub: str = "", analysis: str = "") -> str:
    """One numbered section: the facts, then the reading of them.

    Every section is built the same way — tables first, and below them a block a person or a model
    wrote. The two are kept visually apart because they answer to different things: a number above
    the line has one right answer and can be recomputed from the run folder, while a sentence below
    it is a judgement and is only ever as good as whoever made it. A reader deciding how much
    weight to give a claim should not have to check which program produced the file.

    Sections with nothing to read into them pass no `analysis` and simply end after their tables.
    """
    read = (f'<div class="analysis"><div class="by">Analysis &mdash; written, not measured</div>'
            f"{analysis}</div>") if analysis else ""
    return (f"<section>"
            f'<div class="shead">'
            f'<h2><span class="no">&sect;&thinsp;{n}</span>{esc(title)}</h2>'
            f"{f'<p class=sub>{sub}</p>' if sub else ''}</div>"
            f"{body}{read}</section>")


def document(title: str, sections: Sequence[str], footer: str = "") -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?\
family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@600&\
family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style></head>
<body><main class="wrap">{"".join(sections)}{footer}</main></body></html>
"""


CSS = """
:root{
  --bg:#f4f5f7; --surface:#ffffff; --surface-2:#eceef1;
  --ink:#151a21; --ink-muted:#5a6472;
  --accent:#a8570c; --accent-soft:#f4e3cd;
  --line:#d7dbe1; --good:#3f7d4f; --critical:#b23b3b;
  --font-display:'IBM Plex Sans Condensed',ui-sans-serif,system-ui,sans-serif;
  --font-body:'IBM Plex Sans',ui-sans-serif,system-ui,sans-serif;
  --font-mono:'IBM Plex Mono',ui-monospace,'SFMono-Regular',Menlo,monospace;
}
/* The Blueprint's own dark values, not an inversion of the light ones. A report and the design
   it came from should not part company when someone's OS is in dark mode. */
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#11151c; --surface:#171d27; --surface-2:#1d2430;
    --ink:#e8ecf2; --ink-muted:#8b96a5;
    --accent:#f0a446; --accent-soft:#3a2a15;
    --line:#2a323f; --good:#6fbf80; --critical:#e06868;
  }
}
:root[data-theme="dark"]{
  --bg:#11151c; --surface:#171d27; --surface-2:#1d2430;
  --ink:#e8ecf2; --ink-muted:#8b96a5;
  --accent:#f0a446; --accent-soft:#3a2a15;
  --line:#2a323f; --good:#6fbf80; --critical:#e06868;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--font-body);
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:1060px;margin:0 auto;padding:44px 20px 80px;
  display:flex;flex-direction:column;gap:34px}
h1,h2{margin:0;font-family:var(--font-display);font-weight:600;text-wrap:balance}
h1{font-size:31px;line-height:1.2}
h2{font-size:21px}
p{margin:0;max-width:74ch}
code{font-family:var(--font-mono);font-size:.88em;background:var(--surface-2);
  padding:1px 5px;border-radius:2px}
.n{font-family:var(--font-mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.dash{color:var(--ink-muted)}
.d{font-family:var(--font-mono);font-variant-numeric:tabular-nums;font-size:12.5px}
/* Up and down are not good and bad — more cells reached is better, more failures is worse — so
   they are only ever direction. The reading of a direction is the section's analysis to give. */
.d.up{color:var(--accent)}
.d.down{color:var(--ink-muted)}
.d.flat{color:var(--ink-muted)}
.d.new{color:var(--accent);font-style:italic}

header{display:flex;flex-direction:column;gap:11px}
.eyebrow{font-family:var(--font-mono);font-size:11px;letter-spacing:.15em;
  text-transform:uppercase;color:var(--ink-muted)}

section{display:flex;flex-direction:column;gap:15px}
.shead{display:flex;flex-direction:column;gap:4px;border-bottom:2px solid var(--line);
  padding-bottom:9px}
.shead h2{display:flex;align-items:baseline;gap:11px;font-weight:600}
/* The number is part of the heading, not a label above it, so it takes the heading's size.
   Only the face and the colour set it apart. */
.shead .no{font-family:var(--font-mono);font-size:1em;color:var(--accent);font-weight:600}
.shead .sub{font-size:13.5px;color:var(--ink-muted)}
.by{font-family:var(--font-mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--accent);margin-top:3px}
.analysis{background:var(--accent-soft);border-left:3px solid var(--accent);
  border-radius:0 3px 3px 0;padding:13px 16px;display:flex;flex-direction:column;gap:8px}
.analysis .by{font-family:var(--font-mono);font-size:10px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--accent)}
.analysis p{font-size:13.5px}

.tw{overflow-x:auto;border:1px solid var(--line);border-radius:3px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:13.5px;min-width:520px}
th{font-family:var(--font-display);text-align:left;font-size:11px;letter-spacing:.07em;
  text-transform:uppercase;color:var(--ink-muted);font-weight:600;
  padding:10px 14px 8px;border-bottom:1px solid var(--line);white-space:nowrap;
  background:var(--surface-2)}
td{padding:7px 14px;border-bottom:1px solid var(--line);vertical-align:baseline}
tbody tr:last-child td{border-bottom:0}
th.r,td.r{text-align:right}
tfoot td{border-top:1px solid var(--line);border-bottom:0;font-weight:600;
  background:var(--surface-2)}
figcaption{font-size:12.5px;color:var(--ink-muted);max-width:76ch}
.tname{margin:0;font-family:var(--font-display);font-size:14px;font-weight:600;
  color:var(--ink)}

/* Five across, two down. Ten fields read as one strip rather than a list, which is what a
   summary is for — but a value like a run folder is 23 characters wide, so the label sits above
   the value rather than beside it and the value is allowed to wrap. */
.summary{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:3px;overflow:hidden}
.summary>div{background:var(--surface);padding:11px 13px;display:flex;
  flex-direction:column;gap:4px;min-width:0}
.summary .k{font-family:var(--font-display);font-size:10px;letter-spacing:.07em;
  text-transform:uppercase;color:var(--ink-muted);font-weight:600;line-height:1.3}
.summary .v{font-family:var(--font-mono);font-size:13px;overflow-wrap:anywhere;line-height:1.35}
.summary .v.ok{color:var(--good)}
.summary .v.bad{color:var(--critical)}
.summary .v.none{color:var(--ink-muted)}

.fig{background:var(--surface);border:1px solid var(--line);border-radius:3px;padding:14px 16px}
.fig .scroll{overflow-x:auto}
.fig svg{display:block;width:100%;min-width:900px;height:auto}

.empty{border:1px dashed var(--line);border-radius:3px;padding:16px 18px;
  color:var(--ink-muted);font-size:13.5px;background:var(--surface)}

footer{border-top:1px solid var(--line);padding-top:16px;font-size:12.5px;
  color:var(--ink-muted);display:flex;flex-direction:column;gap:6px;max-width:78ch}

@media (max-width:900px){ .summary{grid-template-columns:repeat(3,1fr)} }
@media (max-width:760px){
  .summary{grid-template-columns:repeat(2,1fr)}
  .wrap{padding:28px 16px 56px;gap:28px}
  h1{font-size:25px}
}
"""
