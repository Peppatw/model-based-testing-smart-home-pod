"""One renderer for the test-case drawing, because two consumers need the same picture.

The Blueprint's Slide 01 and a run's test report both show the chains a test case walks. Until
this module existed the slide's copy was authored by hand, and it drifted: it still draws a
five-state Bluetooth chain that `policy.CHAINS` reduced to three, and calls Matter's third state
`Commissioned` where the code says `Operational`. Nothing caught it, because
`test_consistency.py` compares the code against the tables in `docs/data_structure.md` and never
reads the drawing.

So the drawing is generated, from the same `chains` the engine rolls. A figure that is derived
cannot disagree with the model it depicts — which is a stronger guarantee than any test, and the
reason this is a module rather than a script.

Geometry is measured from Slide 01 rather than invented, so a reader who knows the slide
recognises the output: four clusters in a 2x2 grid, each chain's states on a ring starting at
twelve o'clock and going clockwise, and the fault actions on a ring of their own around the DUT,
which has no states to sit in.

    from pod_mbt.figure import chain_figure
    svg = chain_figure(CHAINS, FAULTS, scheduled={"inject_wifi_drop", ...})
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional, Sequence

# --- measured off docs/pod_mbt_blueprint.html, Slide 01 figure `f1` ------------------
NODE_R = 26          # state circle
RING_3 = 115         # ring radius for a three-state chain; +17.5 per extra state
FAULT_R = 172        # radius of the fault ring
FAULT_NODE_R = 3.5
DUT_R = 24
# Both ends of a cross edge are rotated off the centre-to-centre line by this much, in the
# same rotational sense, so an A->B edge and a B->A edge do not overlap.
EDGE_SKEW = math.radians(18.7)
# A cross edge's control point is pulled from the chord's midpoint TOWARDS the cluster centre,
# which is what makes a cluster read as a rosette rather than a blob.
EDGE_BOW = 24
SELF_SPREAD = math.radians(25)   # a self-loop's feet, either side of the outward radial
SELF_REACH = 50                  # how far past the rim its control point sits

# Wider than Slide 01's original, because each component now carries its own faults on
# its outer side and their labels reach past where the chains alone ended.
# Five clusters: three chains, the system conditions, and everything an external rig
# fires. Wider and taller than the four-cluster original to hold the fifth.
VIEWBOX = "-90 -22 1210 1230"
CENTRES = {0: (205, 280), 1: (715, 280), 2: (205, 800), 3: (715, 800)}
# The fault rig sits below all four, because it fires at the whole test environment
# rather than at one region.
RIG_Y = 1270

PALETTE = {
    "ink": "var(--ink)", "muted": "var(--ink-muted)", "accent": "var(--accent)",
    "surface": "var(--surface-2)", "fault": "var(--accent)", "fault_bg": "var(--accent-soft)",
    "mono": "var(--font-mono)", "display": "var(--font-display)",
}


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def chain_figure(chains: Mapping[str, Mapping[str, Iterable[Any]]],
                 faults: Iterable[str],
                 scheduled: Optional[Iterable[str]] = None,
                 palette: Optional[Mapping[str, str]] = None,
                 strip: Iterable[str] = ("api_", "inject_"),
                 executor_of: Optional[Mapping[str, str]] = None,
                 component_of: Optional[Mapping[str, str]] = None) -> str:
    """Draw `chains` and `faults` as Slide 01 draws them.

    `chains` is `{component: {state: [Transition, ...]}}` — anything with `.action`, `.nxt` and
    `.p`. `faults` are the actions no chain contains. `scheduled`, when given, is the subset a
    particular run actually fired: everything outside it is dimmed and labelled, because a fault
    the model owns and the plan never fires is a gap in the plan, not in the device, and the
    figure is the cheapest place to notice it.

    `strip` removes naming prefixes from edge labels only. The nodes and the fault ring keep the
    real action names, so a reader can grep for them.

    `executor_of` maps each fault to `equipment` or `dut`, and the drawing separates them. Faults
    used to radiate outward from the DUT, which reads as the device causing them; they are drawn
    arriving instead, because a fault lands on the device. But not all of them arrive from
    outside: seven of the eighteen are things the Pod is perfectly capable of doing to itself,
    like being handed a wrong password, and a bench needs no extra hardware for those. A solid
    spoke is equipment; a dashed one is the device to itself.

    The grouping is by **who performs the action**. `api_inject_wrong_psk` and
    `inject_ap_psk_changed` are both "the password is wrong"; one is software on the device and one
    means somebody reconfigures the AP, and a drawing that puts them together hides the only
    difference that costs money. So a call the device makes belongs on its own region's ring as a
    transition, and everything an external rig fires radiates from one hub — which is also the
    hardware list a bench needs before this test case can run.

    A rig spoke arrives at no particular state, because a fault lands on whatever state the device
    happens to be in. That is what makes it a fault and not a transition.

    """
    P = dict(PALETTE, **(palette or {}))
    fired = set(scheduled) if scheduled is not None else None
    short = lambda a: next((a[len(p):] for p in strip if a.startswith(p)), a)
    o: list[str] = [
        '<defs><marker id="tcah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" '
        f'fill="{P["muted"]}"/></marker></defs>']

    def lane(cx: float, cy: float, ring: float, text: str, colour: str, sub: str = "") -> None:
        y = cy - (ring + NODE_R + 62)
        o.append(f'<text x="{cx:.0f}" y="{y:.0f}" text-anchor="middle" font-size="11" '
                 f'font-weight="600" fill="{colour}" font-family="{P["display"]}" '
                 f'letter-spacing="0.9">{_esc(text)}</text>')
        if sub:
            o.append(f'<text x="{cx:.0f}" y="{y + 15:.0f}" text-anchor="middle" font-size="9.5" '
                     f'fill="{P["muted"]}" font-family="{P["display"]}">{_esc(sub)}</text>')

    def label(x: float, y: float, act: str, p: float, anchor: str) -> None:
        o.append(f'<text x="{x:.0f}" y="{y:.0f}" text-anchor="{anchor}" font-size="9.5" '
                 f'fill="{P["muted"]}" font-family="{P["mono"]}">{_esc(act)}</text>')
        o.append(f'<text x="{x:.0f}" y="{y + 12:.0f}" text-anchor="{anchor}" font-size="11" '
                 f'fill="{P["accent"]}" font-family="{P["mono"]}" font-weight="600">{p:.2f}</text>')

    fig_cx = sum(CENTRES[i][0] for i in range(4)) / 4
    fig_cy = sum(CENTRES[i][1] for i in range(4)) / 4
    comp_of = dict(component_of or {})
    by_comp: dict[str, list[str]] = {}
    for f in faults:
        if (executor_of or {}).get(f) == "equipment":
            continue          # the rig has its own band; a region shows only what it does itself
        by_comp.setdefault(comp_of.get(f, "system"), []).append(f)

    for i, (comp, rows) in enumerate(chains.items()):
        cx, cy = CENTRES[i]
        states = list(rows)
        ring = RING_3 + max(0, len(states) - 3) * 17.5
        lane(cx, cy, ring, comp.upper(), P["muted"])
        ang = {s: math.radians(-90) + 2 * math.pi * k / len(states)
               for k, s in enumerate(states)}
        pos = {s: (cx + ring * math.cos(ang[s]), cy + ring * math.sin(ang[s])) for s in states}

        loops: dict[str, int] = {}
        for s in states:
            x1, y1 = pos[s]
            for t in rows[s]:
                if t.nxt == s:
                    # Fan repeat self-loops off the radial so two of them do not print on top
                    # of each other — the code's wifi chain has two from `Disconnected`.
                    k = loops.get(s, 0)
                    loops[s] = k + 1
                    phi = ang[s] + (0.0 if k == 0 else (0.52 if k % 2 else -0.52) * ((k + 1) // 2))
                    sx = x1 + 28.6 * math.cos(phi + SELF_SPREAD)
                    sy = y1 + 28.6 * math.sin(phi + SELF_SPREAD)
                    ex = x1 + 28.6 * math.cos(phi - SELF_SPREAD)
                    ey = y1 + 28.6 * math.sin(phi - SELF_SPREAD)
                    qx = x1 + (NODE_R + SELF_REACH) * math.cos(phi)
                    qy = y1 + (NODE_R + SELF_REACH) * math.sin(phi)
                    o.append(f'<path d="M{sx:.0f},{sy:.0f} Q{qx:.0f},{qy:.0f} {ex:.0f},{ey:.0f}" '
                             f'fill="none" stroke="{P["muted"]}" stroke-width="1.3" '
                             f'marker-end="url(#tcah)"/>')
                    lx = x1 + (NODE_R + 64) * math.cos(phi)
                    ly = y1 + (NODE_R + 64) * math.sin(phi)
                    c = math.cos(phi)
                    anchor = "middle" if abs(c) < 0.40 else ("start" if c > 0 else "end")
                    dy = (-4 if math.sin(phi) < 0 else 14) if abs(c) < 0.40 else 3
                    label(lx, ly + dy, short(t.action), t.p, anchor)
                else:
                    x2, y2 = pos[t.nxt]
                    d = math.atan2(y2 - y1, x2 - x1)
                    sx, sy = x1 + NODE_R * math.cos(d + EDGE_SKEW), y1 + NODE_R * math.sin(d + EDGE_SKEW)
                    rv = d + math.pi
                    ex = x2 + (NODE_R + 1) * math.cos(rv - EDGE_SKEW)
                    ey = y2 + (NODE_R + 1) * math.sin(rv - EDGE_SKEW)
                    mx, my = (sx + ex) / 2, (sy + ey) / 2
                    vx, vy = cx - mx, cy - my
                    n = math.hypot(vx, vy) or 1.0
                    o.append(f'<path d="M{sx:.0f},{sy:.0f} '
                             f'Q{mx + vx / n * EDGE_BOW:.0f},{my + vy / n * EDGE_BOW:.0f} '
                             f'{ex:.0f},{ey:.0f}" fill="none" stroke="{P["muted"]}" '
                             f'stroke-width="1.3" marker-end="url(#tcah)"/>')
                    c = -vx / n
                    anchor = "middle" if abs(c) < 0.40 else ("start" if c > 0 else "end")
                    label(mx - vx / n * 26, my - vy / n * 26, short(t.action), t.p, anchor)

        # This component's faults, arriving from the side that faces away from the figure's
        # middle, so two clusters never reach into each other.
        mine = by_comp.get(comp, [])
        if mine:
            away = math.atan2(cy - fig_cy, cx - fig_cx)
            spread = math.radians(118)
            fr = ring + NODE_R + 72
            for k, f in enumerate(mine):
                a = away + (spread * ((k / (len(mine) - 1)) - 0.5) if len(mine) > 1 else 0)
                fx, fy = cx + fr * math.cos(a), cy + fr * math.sin(a)
                tx, ty = cx + (ring + NODE_R + 9) * math.cos(a), cy + (ring + NODE_R + 9) * math.sin(a)
                on = fired is None or f in fired
                equip = (executor_of or {}).get(f, "equipment") == "equipment"
                colour = P["fault"] if on else P["muted"]
                o.append(f'<line x1="{fx:.0f}" y1="{fy:.0f}" x2="{tx:.0f}" y2="{ty:.0f}" '
                         f'stroke="{colour}" stroke-width="{1.2 if equip else 1}"'
                         f'{"" if equip else " stroke-dasharray=\"2 4\""} '
                         f'opacity="{(0.7 if equip else 0.45) if on else 0.2}" '
                         f'marker-end="url(#tcah)"/>')
                o.append(f'<circle cx="{fx:.0f}" cy="{fy:.0f}" r="{FAULT_NODE_R}" '
                         f'fill="{colour if equip else P["surface"]}" stroke="{colour}" '
                         f'stroke-width="1.2" opacity="{1 if on else 0.4}"/>')
                c = math.cos(a)
                anchor = "middle" if abs(c) < 0.30 else ("start" if c > 0 else "end")
                lx = fx + (12 if c > 0.30 else (-12 if c < -0.30 else 0))
                ly = fy + (3 if abs(c) >= 0.30 else (-8 if math.sin(a) < 0 else 13))
                o.append(f'<text x="{lx:.0f}" y="{ly:.0f}" text-anchor="{anchor}" '
                         f'font-size="8.5" fill="{P["ink"] if on else P["muted"]}" '
                         f'font-family="{P["mono"]}"{"" if on else " opacity=\"0.6\""}>'
                         f"{_esc(f)}</text>")
                if not on:
                    o.append(f'<text x="{lx:.0f}" y="{ly + 10:.0f}" text-anchor="{anchor}" '
                             f'font-size="8" fill="{P["fault"]}" '
                             f'font-family="{P["display"]}">not scheduled</text>')

        for s in states:
            x, y = pos[s]
            o.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{NODE_R}" fill="{P["surface"]}" '
                     f'stroke="{P["ink"]}" stroke-width="1.4"/>')
            parts = s.split(" · ")
            if len(parts) == 1:
                o.append(f'<text x="{x:.0f}" y="{y + 4:.0f}" text-anchor="middle" font-size="9" '
                         f'fill="{P["ink"]}" font-family="{P["mono"]}" font-weight="600">'
                         f'{_esc(s)}</text>')
            else:
                o.append(f'<text x="{x:.0f}" y="{y - 1:.0f}" text-anchor="middle" font-size="9" '
                         f'fill="{P["ink"]}" font-family="{P["mono"]}" font-weight="600">'
                         f'{_esc(parts[0])}</text>')
                o.append(f'<text x="{x:.0f}" y="{y + 10:.0f}" text-anchor="middle" font-size="8" '
                         f'fill="{P["muted"]}" font-family="{P["mono"]}">{_esc(parts[1])}</text>')

    # ---- the fault rig: one hub, spokes radiating out to what it can reach ---------------------
    # Drawn from the centre outward: the rig is one thing with many reaches, and a reader following
    # a spoke is following "this bench can do that". Only `executor: equipment` sits here — what
    # the device does to itself is a call it makes, so it belongs on its own region's ring as a
    # transition, not on a spoke from a rig that has nothing to do with it.
    cx, cy = CENTRES[len(list(chains))]
    rig = [f for f in faults if (executor_of or {}).get(f, "equipment") == "equipment"]
    if rig:
        lane(cx, cy, FAULT_R - NODE_R + FAULT_NODE_R, "FAULT INJECT", P["fault"],
             f"{len(rig)} actions an external rig fires &mdash; the hardware this test case needs")
        for k, f in enumerate(rig):
            a = math.radians(-90) + 2 * math.pi * k / len(rig)
            fx, fy = cx + FAULT_R * math.cos(a), cy + FAULT_R * math.sin(a)
            on = fired is None or f in fired
            colour = P["fault"] if on else P["muted"]
            o.append(f'<line x1="{cx + DUT_R * math.cos(a):.0f}" '
                     f'y1="{cy + DUT_R * math.sin(a):.0f}" x2="{fx:.0f}" y2="{fy:.0f}" '
                     f'stroke="{colour}" stroke-width="1" stroke-dasharray="2 4" '
                     f'opacity="{0.5 if on else 0.22}"/>')
            o.append(f'<circle cx="{fx:.0f}" cy="{fy:.0f}" r="{FAULT_NODE_R}" fill="{colour}" '
                     f'opacity="{1 if on else 0.4}"/>')
            lx, ly = cx + (FAULT_R + 17) * math.cos(a), cy + (FAULT_R + 17) * math.sin(a)
            c = math.cos(a)
            anchor = "middle" if abs(c) < 0.30 else ("start" if c > 0 else "end")
            dy = ((-7 if math.sin(a) < 0 else 13) if abs(c) < 0.30 else 3)
            o.append(f'<text x="{lx:.0f}" y="{ly + dy:.0f}" text-anchor="{anchor}" '
                     f'font-size="8.5" fill="{P["ink"] if on else P["muted"]}" '
                     f'font-family="{P["mono"]}"{"" if on else " opacity=\"0.6\""}>'
                     f"{_esc(f)}</text>")
            if not on:
                o.append(f'<text x="{lx:.0f}" y="{ly + dy + 10:.0f}" text-anchor="{anchor}" '
                         f'font-size="8" fill="{P["fault"]}" '
                         f'font-family="{P["display"]}">not scheduled in this run</text>')
        o.append(f'<circle cx="{cx}" cy="{cy}" r="{DUT_R}" fill="{P["fault_bg"]}" '
                 f'stroke="{P["fault"]}" stroke-width="1.9"/>')
        o.append(f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" font-size="10" '
                 f'fill="{P["fault"]}" font-family="{P["mono"]}" font-weight="700">RIG</text>')

    names = ", ".join(chains)
    n_rig = len([f for f in faults if (executor_of or {}).get(f) == "equipment"])
    return (f'<svg viewBox="{VIEWBOX}" xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-label="The {names} chains as rings of states with their transitions and '
            f"probabilities, each with the faults the device inflicts on itself arriving at its "
            f"own ring; the system conditions as levels with no transitions; and {n_rig} actions "
            f'an external rig fires at the test environment in a band below.">'
            + "".join(o) + "</svg>")
