"""Reading a device's resource numbers, and deciding whether a series is a leak.

Two things, both used from elsewhere and neither with a command line of its own.

**The parsers** are how `Driver.sample_resources` reads a real bench. They are here rather than
inline because the file formats have edges worth writing down once: `/proc/stat` is a cumulative
counter so a CPU percentage needs two reads, and a thread's `comm` field is parenthesised and may
itself contain spaces and `)`, so splitting it on whitespace breaks on a thread called
`Binder:1234_2)`. `top` is deliberately not used — its first iteration reports an average since
boot, and its columns differ between busybox and procps.

**The fit** is what `analyze.py` reads a memory series with. Memory is an accumulating level, so
a straight line fits it closely and a growth figure means something. Three gates, not one: the
growth threshold is the question asked, `min_r2` asks whether a straight line describes the series
at all before anyone reads its slope, and `floor` keeps a series too small to judge from producing
a large percentage of very little. A fourth test separates a leak from a bounded cache filling up:
both clear a growth threshold, and only the last third's slope tells them apart.

Per-thread CPU is measurable with the parsers below and is deliberately not fitted. A thread's
mean is dominated by how many bursts landed in the sampling window, so a rise in its idle floor
arrives diluted by the burst share — four instruments were tried on one session and none separated
planted floor drifts from flat threads, and longer runs, one-second sampling and per-process
aggregation rescued none of them. See `docs/pending_fixes.md` B1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

CLK_TCK = 100          # jiffies per second; getconf CLK_TCK on the DUT
DEFAULT_INTERVAL_MS = 30_000


def parse_proc_stat(text: str) -> tuple[int, int]:
    """First line of /proc/stat -> (idle_jiffies, total_jiffies), both cumulative.

    CPU% is a delta, so a single call cannot produce one. The caller keeps the
    previous pair; this only reads.
    """
    parts = text.splitlines()[0].split()
    fields = [int(x) for x in parts[1:]]        # user nice system idle iowait irq softirq steal
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return idle, sum(fields)


def parse_task_stat(text: str) -> tuple[int, str, int]:
    """/proc/<pid>/task/<tid>/stat -> (tid, name, utime+stime jiffies), cumulative.

    The comm field is parenthesised and may itself contain spaces and ')', so the
    split is anchored on the LAST ')' rather than on whitespace — the standard
    reason a naive split of this file breaks on threads named like `Binder:1234_2)`.
    """
    tid = int(text[:text.index(" ")])
    close = text.rindex(")")
    name = text[text.index("(") + 1:close]
    rest = text[close + 2:].split()
    utime, stime = int(rest[11]), int(rest[12])   # fields 14 and 15, 1-indexed
    return tid, name, utime + stime


def parse_dumpsys_meminfo(text: str, process: str, pid: int) -> ProcessMem:
    """Read the `App Summary` block of `dumpsys meminfo <pkg>`.

    Only the summary is parsed: the per-mapping table above it moves between Android
    releases, while the summary's row labels have been stable for far longer. A label
    this does not know is skipped rather than guessed at.
    """
    wanted = {"Java Heap:": "java_heap", "Native Heap:": "native_heap",
              "Code:": "code", "Stack:": "stack", "Private Other:": "private_other",
              "TOTAL PSS:": "total_pss", "TOTAL:": "total_pss"}
    out: dict[str, int] = {}
    for line in text.splitlines():
        s = line.strip()
        for label, key in wanted.items():
            if s.startswith(label):
                nums = [t for t in s[len(label):].split() if t.lstrip("-").isdigit()]
                if nums:
                    out.setdefault(key, int(nums[0]))
    return ProcessMem(process=process, pid=pid, total_pss=out.pop("total_pss", 0), **out)


@dataclass
class Trend:
    """A least-squares fit over one series, plus what it takes to trust the fit.

    `growth_pct` is the number the threshold is applied to: the *fitted line's* rise
    across the session as a share of where that line started. Deliberately not
    last-minus-first — one high final sample is not a leak, and on a noisy series
    last-minus-first can carry either sign at random.
    """
    key: str
    unit: str
    first_fit: float
    last_fit: float
    growth_pct: float
    slope_per_hour: float
    r_squared: float
    mean: float
    n: int
    flagged: bool = False
    reason: str = ""
    # Slope of the last third over the slope of the whole, for series that are levels.
    # A leak keeps its slope to the end; a cache filling up loses it. Both clear a
    # growth threshold, so without this the two are the same finding.
    tail_ratio: Optional[float] = None
    shape: str = ""


def fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Ordinary least squares -> (intercept, slope, r_squared). No numpy."""
    n = len(xs)
    if n < 3:
        return (ys[0] if ys else 0.0), 0.0, 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return intercept, slope, r2


def smooth(ys: list[float], window: int) -> list[float]:
    """Centred rolling mean. Fit this, not the raw series.

    At a 30-second interval a single sample is one 30-second slice of a thread that
    may not have been scheduled much in it, so the per-sample variance is comparable
    to the drift being looked for — which is what makes a raw fit report R^2 near zero
    on a series that is genuinely climbing. Averaging over a few minutes removes that
    variance without touching the slope: the trend survives the filter and the jitter
    does not. A series with no trend stays flat after smoothing, so this does not
    manufacture findings — it only stops real ones from being buried.
    """
    if window <= 1 or len(ys) < window:
        return list(ys)
    half, out = window // 2, []
    for i in range(len(ys)):
        lo, hi = max(0, i - half), min(len(ys), i + half + 1)
        out.append(sum(ys[lo:hi]) / (hi - lo))
    return out


def trend(key: str, unit: str, t_hours: list[float], ys: list[float],
          growth_threshold: float = 10.0, min_r2: float = 0.35,
          floor: float = 0.0, window: int = 1, shaped: bool = False) -> Trend:
    """One series -> one verdict.

    Three gates, not one. The growth threshold is the question asked; `min_r2` and
    `floor` are there because on their own a percentage lies in two specific ways:

    * A thread averaging 0.04% CPU that drifts to 0.05% has grown 25% and means
      nothing — hence `floor`, an absolute level below which a series is not worth
      a verdict at all.
    * A series with large variance and no direction produces a non-zero slope every
      time it is fitted; the sign is whatever the noise happened to do — hence
      `min_r2`, which asks whether a straight line describes this series before
      anyone reads its slope.
    """
    intercept, slope, r2 = fit(t_hours, smooth(ys, window))
    first_fit, last_fit = intercept + slope * t_hours[0], intercept + slope * t_hours[-1]
    mean = sum(ys) / len(ys) if ys else 0.0
    growth = 100.0 * (last_fit - first_fit) / first_fit if first_fit > 1e-9 else 0.0

    t = Trend(key=key, unit=unit, first_fit=round(first_fit, 4),
              last_fit=round(last_fit, 4), growth_pct=round(growth, 2),
              slope_per_hour=round(slope, 5), r_squared=round(r2, 4),
              mean=round(mean, 4), n=len(ys))
    if shaped and len(ys) >= 30:
        sm = smooth(ys, window)
        k = len(sm) * 2 // 3
        _, tail_slope, _ = fit(t_hours[k:], sm[k:])
        t.tail_ratio = round(tail_slope / slope, 3) if abs(slope) > 1e-9 else 0.0
        t.shape = "still climbing" if t.tail_ratio > 0.45 else (
            "levelled off" if t.tail_ratio > -0.2 else "falling back")

    if mean < floor:
        t.reason = f"below floor ({mean:.3g} < {floor:g} {unit}) — too small to judge"
    elif growth < growth_threshold:
        t.reason = f"growth {growth:.1f}% < {growth_threshold:g}%"
    elif r2 < min_r2:
        t.reason = f"growth {growth:.1f}% but R^2 {r2:.2f} < {min_r2:g} — no linear trend"
    elif t.shape == "levelled off":
        t.reason = (f"grew {growth:.1f}% but stopped: last third at "
                    f"{t.tail_ratio:.0%} of the session slope — a bounded cache, not a leak")
    else:
        t.flagged = True
        t.reason = f"growth {growth:.1f}% over the session, R^2 {r2:.2f}"
        if t.tail_ratio is not None:
            t.reason += f", still climbing at the end ({t.tail_ratio:.0%} of slope)"
    return t
