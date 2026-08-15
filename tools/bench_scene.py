#!/usr/bin/env python3
"""Render-cost benchmark: how big a scene can the engine actually draw?

The repo has no test suite and no timing harness, so every performance number
in TECHNICAL.md is prose from a one-off measurement that can't be re-checked.
This makes the measurement repeatable — run it before and after any change to
`scene3d.py` or `generators/world.py` and compare.

It answers the question "can this scene hold framerate?" by rendering real
frames from a real scene and timing them, rather than reasoning about it. To
explore sizes the library doesn't contain, it synthesises bigger variants IN
MEMORY: the node list is duplicated `--mult` times with each copy offset in x/z,
which grows the world the way a bigger authored scene would without needing one
to exist. Nothing is written to disk and no network call is made.

    .venv/bin/python tools/bench_scene.py --scene pottery
    .venv/bin/python tools/bench_scene.py --scene pottery --mult 1,3,5 --strokes 120,300,600
    .venv/bin/python tools/bench_scene.py --scene ants --fps 30

What the numbers mean
---------------------
`drawn` is the count actually emitted after the camera's frustum cull and
stroke budget — usually well below `max_strokes`, because most of a world is
off-screen at any moment. It is the number that costs: measured on this
codebase a drawn stroke runs ~0.12ms against ~0.014ms for a node, so stroke
count dominates and node count is nearly free (that asymmetry is the whole
point of `World._render_budgeted`'s node-level culling).

`peak` matters more than `avg`. A frame over budget is a dropped frame, and
one slow frame in twenty is visible as a stutter even when the mean looks fine.

Two measurement traps this works around, both found by measuring the same
config twice and getting different answers:

* **Sample long enough to travel.** An orbit/drift/path camera moves, so how
  much geometry is on screen depends on where along its route it is. A
  one-second sample of `pottery` reports ~14ms; the same scene over four
  seconds reports ~30ms, because the short window never reaches the dense part
  of the route. `--seconds` is scene-time travelled, and short values
  under-report.
* **Warm up properly.** The first measured run of a process came out ~40%
  above the settled figure even after ten warmup frames (43ms then 29-32ms).
  A full second of warmup frames removes it.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from promptwaver.perf import LoopStats          # noqa: E402
from promptwaver.scenes import Scene, SceneSpec  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENES = os.path.join(HERE, "scenes")


def load(name: str) -> dict:
    path = os.path.join(SCENES, f"{name}.json")
    if not os.path.exists(path):
        sys.exit(f"no such scene: {path}")
    with open(path) as f:
        return json.load(f)


def _offsets(n: int, step: float = 9.0) -> list[tuple[float, float]]:
    """Copy placements in rings outward from the origin, **starting at (0, 0)**.

    The first copy must land exactly where the original was: at `--mult 1` the
    benchmark's baseline row has to be the real scene, or every comparison is
    against a fiction. (An earlier version offset even the first copy, which
    moved the whole world out from under a path camera and made `pottery`
    report 12ms where the untouched scene costs ~30ms.)
    """
    out: list[tuple[float, float]] = []
    ring = 0
    while len(out) < n:
        for gx in range(-ring, ring + 1):
            for gz in range(-ring, ring + 1):
                if max(abs(gx), abs(gz)) == ring:
                    out.append((gx * step, gz * step))
        ring += 1
    return out[:n]


def grow(spec: dict, mult: int, max_strokes: int | None) -> tuple[dict, int]:
    """`mult` copies of every node, spread on a grid so the copies occupy new
    space rather than stacking invisibly inside each other — piling them at the
    same coordinates would grow the node count while leaving the *drawn* count
    unchanged, which measures nothing."""
    d = copy.deepcopy(spec)
    total = 0
    offs = _offsets(mult)
    for layer in d.get("layers", []):
        nodes = (layer.get("params") or {}).get("nodes")
        if not nodes:
            continue
        grown = []
        for dx, dz in offs:
            for nd in nodes:
                c = copy.deepcopy(nd)
                pos = c.get("pos") or [0, 0, 0]
                c["pos"] = [pos[0] + dx, pos[1], pos[2] + dz]
                grown.append(c)
        layer["params"]["nodes"] = grown
        total += len(grown)
    if max_strokes is not None:
        d.setdefault("camera", {})["max_strokes"] = max_strokes
    return d, total


def measure(spec: dict, fps: int, seconds: float) -> tuple[dict, int]:
    """Render frames at the nominal frame interval and time each one.

    Uses `LoopStats` — the same accounting the live engine reports through
    `state()["perf_diag"]` — so a number here is directly comparable with what
    Settings > Diagnostics shows, instead of being a parallel definition of
    "render time" that might drift from it.
    """
    scene = Scene(SceneSpec.from_dict(spec))
    stats = LoopStats(fps)
    dt = 1.0 / fps
    t = 0.0
    # A full second of warmup, not a handful of frames. Two costs settle here:
    # the def/primitive geometry caches, which are built on first sight and
    # would otherwise be charged as if they recurred every frame; and a
    # process-level warmup effect worth ~40% on the first measured run (see
    # the module docstring).
    for _ in range(fps):
        t += dt
        scene.render(t, dt)

    # Measured in SCENE time, not wall time: the camera advances by dt per
    # frame, so this is "how far along its route did we sample", which is the
    # thing that has to be representative. Wall-clock sampling would cover less
    # of the route on a slow scene — exactly the scenes that need the coverage.
    frames = max(1, int(seconds * fps))
    for _ in range(frames):
        t += dt
        a = time.perf_counter()
        frame = scene.render(t, dt)
        r = time.perf_counter() - a
        stats.tick()
        # output_s is 0: this measures scene cost only. The real loop also
        # spends time in the DAC/point-count pass, which is a property of the
        # output device rather than of the scene.
        # `n_points` is a stroke count despite the name — that is what the live
        # engine feeds it too (`Engine._loop` passes `len(frame)`), so the two
        # readouts stay comparable.
        stats.record(render_s=r, output_s=0.0, total_s=r,
                     n_points=len(frame), crossfading=False)
    summ = stats.summary()
    # The AVERAGE across the sample, not the last frame's count. A travelling
    # camera sees a different amount of the world at every moment, so a single
    # frame's figure is noise — a wide scene sampled at the wrong instant can
    # report zero strokes while costing 34ms in node culling.
    return summ, int(summ["avg_points"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scene", default="pottery", help="scene name in scenes/ (no .json)")
    ap.add_argument("--mult", default="1,3,5", help="world-size multipliers, comma separated")
    ap.add_argument("--strokes", default="", help="max_strokes values; blank = the scene's own")
    ap.add_argument("--fps", type=int, default=45, help="frame budget to judge against")
    ap.add_argument("--seconds", type=float, default=4.0,
                    help="scene-seconds of camera travel to sample per cell; "
                         "short values under-report (see module docstring)")
    a = ap.parse_args()

    base = load(a.scene)
    mults = [int(x) for x in a.mult.split(",") if x.strip()]
    strokes = [int(x) for x in a.strokes.split(",") if x.strip()] or [None]
    budget = 1000.0 / a.fps

    own = (base.get("camera") or {}).get("max_strokes")
    print(f"scene: {a.scene}   fps: {a.fps}   frame budget: {budget:.1f}ms"
          f"   scene's own max_strokes: {own}")
    print(f"{'nodes':>6} {'max_strk':>8} {'drawn':>6} {'avg ms':>7} {'peak ms':>8} "
          f"{'drop%':>6}  verdict")

    for m in mults:
        for s in strokes:
            spec, n = grow(base, m, s)
            summ, drawn = measure(spec, a.fps, a.seconds)
            avg, peak = summ["avg_render_ms"], summ["max_render_ms"]
            # Judged on peak, not mean: one frame over budget is one dropped
            # frame, and a scene that only *usually* fits still stutters.
            verdict = ("ok" if peak < budget
                       else "tight" if avg < budget
                       else "OVER BUDGET")
            print(f"{n:>6} {str(s if s is not None else own):>8} {drawn:>6} "
                  f"{avg:>7.1f} {peak:>8.1f} {summ['dropped_pct']:>6.1f}  {verdict}")


if __name__ == "__main__":
    main()
