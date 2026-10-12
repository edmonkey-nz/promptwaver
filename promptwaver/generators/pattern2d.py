"""Flat symmetric patterns — mandalas, kaleidoscopes, neon lattices.

The 2D sibling of `world`: both are DECLARATIVE interpreters over authored
data (`defs` + `nodes`) rather than a fixed algorithm with knobs, which is the
shape that actually survived contact with the scene director. The difference
is that this one has no camera — the pattern is composed directly in the frame
in normalized [-1,1] and never moves through space, so what you author is
exactly what fills the output.

    "layers": [{"generator": "pattern2d", "params": {
      "defs": {
        "arm":     {"space":"cart",  "ops":[{"op":"line","a":[0.18,0.18],"b":[0.18,0.95]}]},
        "diamond": {"space":"polar", "ops":[{"op":"ngon","n":4,"r":0.12}]}
      },
      "nodes": [
        {"def":"arm", "color":[0.3,0.8,1.0], "glow":0.7,
         "repeat":{"kind":"offset","d":0.045,"n":4,"hue_step":0.06},
         "symmetry":{"mirror":"xy"}},
        {"def":"diamond", "color":[0.7,0.3,1.0],
         "repeat":{"kind":"scale","factor":1.5,"n":3,"hue_step":0.1}}
      ]}}]

See patterns2d.py for the op/space/combinator vocabulary. Node keys:

  def/shape   which entry in `defs`
  at          [x, y] placement; or `at_polar` [radius, angle-in-turns]
  scale       scalar, applied about the node's own origin
  rotate      turns
  color       [r, g, b] 0..1 — the base a repeat's `hue_step` rotates away from
  glow        0..1 per-shape bloom (monitor only; see geometry.Path.glow)
  repeat      one of offset / scale / radial / ring / grid
  symmetry    mirror x|y|xy and/or radial n

The top-level `scale`, `rotate`, `glow` and `spread` params are plain scalars
ON PURPOSE: Scene._resolve pushes every top-level param through the modulation
matrix as `visual.<key>`, so those four are audio- and LFO-modulatable with no
further wiring, while anything buried in `defs`/`nodes` is not.
"""

from __future__ import annotations

import numpy as np

from ..color import hue_shift
from ..geometry import Path, Frame
from ..patterns2d import build_def, apply_repeat, apply_symmetry, _rot_matrix
from .base import Generator, register


@register("pattern2d")
class Pattern2D(Generator):
    description = "flat symmetric line pattern — mandala, kaleidoscope, neon lattice"
    defaults = dict(
        defs={}, nodes=[],
        scale=1.0,        # whole-pattern zoom
        rotate=0.0,       # whole-pattern spin, in turns
        spread=1.0,       # scales node PLACEMENT only, not node size
        glow=0.0,         # BOOST added to every node's own glow (see render)
        max_strokes=420,  # hard ceiling; see _emit
    )
    param_meta = {
        # up to 5x so a dense pattern can be zoomed right into for detail
        "scale": (0.1, 5.0, 0.01),
        "rotate": (0.0, 1.0, 0.002),
        "spread": (0.0, 3.0, 0.01),
        "glow": (0.0, 1.0, 0.01),
        "max_strokes": (20, 1200, 10),
    }

    def __init__(self, **params):
        super().__init__(**params)
        # defs are static — only the node transform and modulation move — so
        # the expanded motif geometry is built once and reused, same reasoning
        # as World._def_cache.
        self._def_cache: dict[str, list[np.ndarray]] = {}
        self._cache_key = None

    def _geom_for(self, name: str, defs: dict) -> list[np.ndarray]:
        if self._cache_key is not id(defs):
            self._def_cache.clear()
            self._cache_key = id(defs)
        hit = self._def_cache.get(name)
        if hit is None:
            hit = self._def_cache[name] = build_def(defs.get(name))
        return hit

    def render(self, t: float, p: dict) -> Frame:
        defs = p.get("defs") or {}
        nodes = p.get("nodes") or []
        g_scale = float(p.get("scale", 1.0))
        g_rot = float(p.get("rotate", 0.0))
        spread = float(p.get("spread", 1.0))
        g_glow = max(0.0, min(1.0, float(p.get("glow", 0.0))))
        budget = max(1, int(p.get("max_strokes", 420)))

        R = _rot_matrix(g_rot).T if g_rot else None
        out: Frame = []

        # Recomputed per frame rather than cached: `nodes` is a live params
        # entry that the UI and the director can both replace, and the scan is
        # a couple of hundred float comparisons against the geometry build
        # below. Warned once so a rescued scene is visible in the log rather
        # than silently "just working" at a scale nobody authored.
        pscale = _placement_scale(nodes)
        if pscale != 1.0 and not getattr(self, "_warned_placement", False):
            self._warned_placement = True
            print(f"[pattern2d] node placements are ~{1/pscale:.0f}x outside the "
                  f"[-1,1] frame — rescaling to fit (scene JSON has the wrong units)")

        for node in nodes:
            if len(out) >= budget:
                break
            name = node.get("def") or node.get("shape")
            base = self._geom_for(name, defs) if name else []
            if not base:
                continue

            items = [(P, 0.0) for P in base]
            items = apply_repeat(items, node.get("repeat"))
            items = apply_symmetry(items, node.get("symmetry"))
            items = _spread(items, spread)

            n_scale = float(node.get("scale", 1.0))
            n_rot = float(node.get("rotate", 0.0))
            NR = _rot_matrix(n_rot).T if n_rot else None
            at = _placement(node) * spread * pscale

            color = node.get("color") or [1.0, 1.0, 1.0]
            # ADDED to the node's own glow, not a floor under it. A floor can
            # never exceed the brightest authored shape, so routing audio at
            # it would visibly do nothing on exactly the scenes that bother to
            # author glow. Adding lifts the whole pattern while preserving the
            # relative differences the scene composed.
            glow = min(1.0, float(node.get("glow", 0.0)) + g_glow)
            closed = bool(node.get("closed", False))

            for P, hue_d in items:
                if len(out) >= budget:
                    break
                Q = P * n_scale
                if NR is not None:
                    Q = Q @ NR
                Q = (Q + at) * g_scale
                if R is not None:
                    Q = Q @ R
                out.append(Path(Q.astype(np.float32),
                                hue_shift(color, hue_d), closed=closed, glow=glow))
        return out


    # --- GPU renderer (see Generator.gpu_tick / gpu_pack) ----------------------

    def gpu_tick(self, t: float, p: dict) -> dict:
        return {"scale": float(p.get("scale", 1.0)), "rotate": float(p.get("rotate", 0.0)),
                "spread": float(p.get("spread", 1.0)),
                "glow": max(0.0, min(1.0, float(p.get("glow", 0.0)))),
                "max_strokes": max(1, int(p.get("max_strokes", 420)))}

    def gpu_pack(self, p: dict) -> dict:
        """Every stroke `render` can emit, in emit order, with the four live
        scalars factored out. `render` computes, per stroke,

            Q = ((P + c*(spread-1)) * ns) @ NR + at*spread*pscale, then
            Q = (Q * scale) @ R(rotate)

        where c is the stroke's own centroid. That is linear in `spread`, so
        each stroke ships `P*ns@NR`, the spread direction `c*ns@NR` and its
        placement `at*pscale`, and the browser rebuilds Q from this tick's
        uniforms (scene-eval.js `evalPattern`). `max_strokes` is a prefix of
        this list, as `render`'s budget is."""
        defs = p.get("defs") or {}
        nodes = p.get("nodes") or []
        pscale = _placement_scale(nodes)
        out = []
        for node in nodes:
            name = node.get("def") or node.get("shape")
            base = self._geom_for(name, defs) if name else []
            if not base:
                continue
            items = [(P, 0.0) for P in base]
            items = apply_repeat(items, node.get("repeat"))
            items = apply_symmetry(items, node.get("symmetry"))
            n_scale = float(node.get("scale", 1.0))
            n_rot = float(node.get("rotate", 0.0))
            NR = _rot_matrix(n_rot).T if n_rot else None
            at0 = _placement(node) * pscale
            color = node.get("color") or [1.0, 1.0, 1.0]
            glow = float(node.get("glow", 0.0))
            closed = bool(node.get("closed", False))
            for P, hue_d in items:
                Q = P * n_scale
                c = P.mean(axis=0) * n_scale
                if NR is not None:
                    Q = Q @ NR
                    c = c @ NR
                out.append((Q, c, at0, hue_shift(color, hue_d), glow, closed))
        return _pack_pattern(out)

def _pack_pattern(base_items_by_node) -> dict:
    """Shared by Pattern2D.gpu_pack and the test pattern: per-stroke points
    plus the terms the browser needs to apply the four live scalars."""
    stroke_i, stroke_f, pts = [], [], []
    n = 0
    for Q0, cterm, at0, color, glow, closed in base_items_by_node:
        stroke_i.append([n, len(Q0), int(closed), 0])
        stroke_f.append([float(cterm[0]), float(cterm[1]), float(at0[0]), float(at0[1]),
                         float(color[0]), float(color[1]), float(color[2]), float(glow)])
        pts.append(np.asarray(Q0, np.float32).reshape(-1, 2))
        n += len(Q0)
    return {
        "kind": "pattern2d",
        "arrays": {
            "stroke_i": np.asarray(stroke_i, np.int32).reshape(-1, 4),
            "stroke_f": np.asarray(stroke_f, np.float32).reshape(-1, 8),
            "pts": np.concatenate(pts) if pts else np.zeros((0, 2), np.float32),
        },
    }


def pack_frame_2d(frame) -> dict:
    """A fixed 2D frame (the test pattern) in pattern2d's wire format, drawn
    with the identity uniforms."""
    z = np.zeros(2, np.float32)
    return _pack_pattern((p.points, z, z, p.color, getattr(p, "glow", 0.0), p.closed)
                         for p in frame)


def _spread(items, spread: float):
    """Push each piece away from the centre WITHOUT resizing it.

    This is the whole difference between `spread` and `scale`: scale zooms
    everything, so shapes grow as they move apart; spread slides them outward
    at constant size, opening gaps in the composition.

    Each piece moves by its own centroid times (spread - 1), so a radial array
    of petals fans outward while a motif already centred on the origin stays
    put. That per-piece centroid is why this runs after repeat/symmetry — the
    copies are what have distinct positions to spread, and an earlier version
    that scaled the repeat's own distance parameters instead was nearly
    invisible on a real scene (a 0.038 band gap has almost nothing to give).
    """
    if spread == 1.0:
        return items
    k = spread - 1.0
    out = []
    for P, h in items:
        c = P.mean(axis=0)
        out.append((P + c * k, h))
    return out


#: Largest node placement, in frame units, still treated as deliberate. The
#: frame is [-1,1] and the prompt asks compositions to reach ~0.9, so a node
#: at 2 or 3 is a plausible authored choice (partly off-screen, or pulled in
#: by `spread`). An order of magnitude beyond that is not a composition, it is
#: a units mistake.
_PLACEMENT_SANE = 3.0


def _placement_scale(nodes) -> float:
    """1.0 normally; a corrective factor when a scene's placements are plainly
    authored in the wrong units.

    Generated scenes occasionally come back with `at` in some other coordinate
    space entirely — a real one measured [-55..65] against a [-1,1] frame, so
    every single motif landed off-screen and the scene rendered pure black
    with no error raised anywhere. The geometry and the composition are fine
    in that case; only the scale is wrong, and it is recoverable exactly
    because it is uniform. Rescaling to fit beats showing nothing, and the
    threshold is high enough that no deliberate composition trips it.
    """
    m = 0.0
    for node in nodes:
        at = node.get("at")
        if isinstance(at, (list, tuple)) and len(at) >= 2:
            for c in at[:2]:
                try:
                    m = max(m, abs(float(c)))
                except (TypeError, ValueError):
                    pass
        ap = node.get("at_polar")
        if isinstance(ap, (list, tuple)) and ap:
            try:
                m = max(m, abs(float(ap[0])))
            except (TypeError, ValueError):
                pass
    return 1.0 if m <= _PLACEMENT_SANE else 0.92 / m


def _placement(node: dict) -> np.ndarray:
    """`at` [x,y], or `at_polar` [radius, angle-in-turns]. Polar placement is
    what makes a ring of motifs natural to author without trigonometry in the
    scene JSON."""
    ap = node.get("at_polar")
    if ap is not None:
        r, turns = (list(ap) + [0.0, 0.0])[:2]
        a = float(turns) * 2 * np.pi
        return np.array([r * np.cos(a), r * np.sin(a)], np.float32)
    return np.asarray(node.get("at") or [0.0, 0.0], np.float32).reshape(2)
