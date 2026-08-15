"""Grow a generated scene to the node count that was asked for.

The measured problem this solves: models do not write the node count they are
told to. Haiku lands near 200 nodes whether asked for 220, 690 or 900 — three
separate runs, same answer — and Sonnet fails the other way, writing past
64,000 output tokens without closing the JSON and being truncated, discarded
and billed in full. So a big world cannot be reached by asking harder, and
paying more does not help either.

It does not need to be. A node is *one placement of an existing shape* —
`{"shape": "gear_large", "pos": [...], "scale": 1.0, "color": [...],
"motion": {...}}` — about 58 tokens of JSON carrying no design decisions the
model has not already made in its `defs` library and its first hundred
placements. The expensive, genuinely creative part (the shape grammar, the
palette, the motion character, the route) is exactly the part that fits
comfortably in one call. Only the repetition is expensive, and repetition is
free locally.

So: let the model author ~200 nodes, then instance its own work along its own
camera route until the world is as big as it was asked to be. 1200 nodes costs
one small API call and a few milliseconds instead of a truncated 73k-token
response.

Why this looks composed rather than scattered
---------------------------------------------
Every new node is a **copy of an authored one**, so its shape, palette and
motion character are the model's, not invented here. What is recomputed is
where it goes — and even that is copied, in the coordinate frame that matters:
each authored node's position is decomposed against the nearest point of the
camera route into (lateral offset, height, forward nudge), and a new instance
keeps those and moves to a different point on the same route.

That decomposition is the whole reason this reads as a place rather than as
noise. A floor plane authored at ground level stays at ground level; a lamp
the model hung 4 units up and 5 to the left of the walkway stays hanging 4 up
and 5 to the left, somewhere else along the walkway. Placing copies at random
positions in the bounding box — the obvious implementation — puts floors in
the air and loses the composition immediately.
"""

from __future__ import annotations

import copy
import random

import numpy as np

#: Only jitter enough to break visible cloning. These are deliberately small:
#: the point is a world that reads as built from a kit of parts, which is what
#: the model was asked for, not one where every copy has drifted into its own
#: thing. Motion speed gets the widest relative range because instances moving
#: in exact lockstep is the single clearest tell that geometry was duplicated.
_SCALE_JITTER = (0.82, 1.24)
_SPEED_JITTER = (0.80, 1.25)
_COLOR_JITTER = 0.05
_LATERAL_JITTER = 1.6     # world units, added to the copied lateral offset
_HEIGHT_JITTER = 0.7


def _closed_route(waypoints) -> np.ndarray | None:
    wp = np.asarray(waypoints, dtype=np.float64)
    if wp.ndim != 2 or wp.shape[0] < 3 or wp.shape[1] != 3:
        return None
    return wp


def _route_from_nodes(nodes: list[dict]) -> np.ndarray | None:
    """A synthetic loop for scenes with no path camera.

    Orbit and drift cameras circle a centre rather than following waypoints,
    and hand-built scenes may have no route at all. A ring through the node
    cloud at its own mean radius is a reasonable stand-in: it puts new
    geometry where the existing geometry already is, which is the property
    that actually matters here.
    """
    pts = np.asarray([n.get("pos", [0, 0, 0]) for n in nodes], dtype=np.float64)
    if len(pts) < 3:
        return None
    centre = pts.mean(axis=0)
    flat = pts[:, [0, 2]] - centre[[0, 2]]
    radius = float(np.linalg.norm(flat, axis=1).mean())
    if radius < 1e-3:
        radius = 1.0
    a = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    return np.stack([centre[0] + radius * np.cos(a),
                     np.full_like(a, centre[1]),
                     centre[2] + radius * np.sin(a)], axis=1)


def _sample(route: np.ndarray, n: int):
    """`n` points evenly spaced along the CLOSED polyline, with unit tangents
    and unit lateral vectors (horizontal, perpendicular to travel).

    Even spacing by arc length, not by waypoint index: waypoints are placed by
    eye and their spacing varies a lot, so indexing by waypoint would bunch
    geometry wherever the model happened to put its points close together.
    """
    pts = np.vstack([route, route[:1]])            # close the loop
    seg = np.diff(pts, axis=0)
    seglen = np.linalg.norm(seg, axis=1)
    keep = seglen > 1e-9
    seg, seglen, pts = seg[keep], seglen[keep], pts[:-1][keep]
    if not len(seg):
        return None, None, None
    cum = np.concatenate([[0.0], np.cumsum(seglen)])
    s = np.linspace(0.0, cum[-1], max(n, 2), endpoint=False)
    i = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(seg) - 1)
    frac = ((s - cum[i]) / seglen[i])[:, None]
    p = pts[i] + seg[i] * frac
    tang = seg[i] / seglen[i][:, None]
    # Lateral is horizontal by construction (cross with world up), so a route
    # that climbs doesn't tilt the geometry beside it off the level.
    lat = np.cross(tang, np.array([0.0, 1.0, 0.0]))
    ln = np.linalg.norm(lat, axis=1, keepdims=True)
    lat = np.where(ln > 1e-9, lat / np.maximum(ln, 1e-9), np.array([1.0, 0.0, 0.0]))
    return p, tang, lat


def _decompose(nodes, p, tang, lat):
    """Each authored node's position in route-relative terms.

    Returns one (lateral, height, forward) triple per node, measured against
    its own nearest route sample. This is what lets a copy keep its
    relationship to the walkway instead of just its coordinates.
    """
    pos = np.asarray([n.get("pos", [0, 0, 0]) for n in nodes], dtype=np.float64)
    # (nodes x samples) distances — a few hundred by a few hundred, so the
    # dense form is faster than anything cleverer at these sizes.
    d = np.linalg.norm(pos[:, None, :] - p[None, :, :], axis=2)
    near = np.argmin(d, axis=1)
    rel = pos - p[near]
    return (np.einsum("ij,ij->i", rel, lat[near]),      # lateral
            rel[:, 1],                                   # height
            np.einsum("ij,ij->i", rel, tang[near]),      # forward
            near)


def expand_nodes(nodes: list[dict], target: int, waypoints=None,
                 seed: int = 0) -> list[dict]:
    """Return `nodes` grown to `target` entries by instancing along the route.

    The authored nodes are kept verbatim and first; everything after them is a
    copy. Returns the input unchanged when it is already big enough, when
    there is nothing to instance, or when no usable route can be built —
    growing a scene is an improvement, never a precondition.
    """
    nodes = list(nodes or [])
    target = int(target)
    if len(nodes) >= target or not nodes:
        return nodes

    route = _closed_route(waypoints) if waypoints is not None else None
    if route is None:
        route = _route_from_nodes(nodes)
    if route is None:
        return nodes

    # Sample the route finely enough that consecutive additions don't land on
    # the same spot, but cap it — beyond a few thousand the decomposition
    # matrix stops being free and the extra resolution buys nothing.
    n_samples = int(min(4000, max(64, target * 3)))
    p, tang, lat = _sample(route, n_samples)
    if p is None:
        return nodes

    lateral, height, forward, _ = _decompose(nodes, p, tang, lat)
    rng = random.Random(seed)
    out = list(nodes)

    # Draw sources by reshuffling the authored list each pass rather than
    # cycling it. Both keep the model's mix of shapes in its original
    # proportions; only the shuffle avoids laying them out in a repeating
    # A-B-C-A-B-C sequence along the route, which is plainly visible when you
    # walk it.
    order: list[int] = []
    need = target - len(nodes)
    for k in range(need):
        if not order:
            order = list(range(len(nodes)))
            rng.shuffle(order)
        src_i = order.pop()
        src = nodes[src_i]

        # Spread additions evenly over the whole route, offset by a fraction
        # of the step so copies don't stack on the authored nodes' own
        # positions.
        j = int((k + 0.5) * n_samples / need + rng.uniform(-0.4, 0.4)
                * n_samples / need) % n_samples

        lat_off = lateral[src_i] + rng.uniform(-_LATERAL_JITTER, _LATERAL_JITTER)
        pos = (p[j] + lat[j] * lat_off + tang[j] * forward[src_i]
               + np.array([0.0, height[src_i]
                           + rng.uniform(-_HEIGHT_JITTER, _HEIGHT_JITTER), 0.0]))

        node = copy.deepcopy(src)
        node["pos"] = [round(float(v), 3) for v in pos]
        node["scale"] = round(float(src.get("scale", 1.0))
                              * rng.uniform(*_SCALE_JITTER), 3)
        col = src.get("color")
        if isinstance(col, (list, tuple)) and len(col) == 3:
            node["color"] = [round(min(1.0, max(0.0, float(c)
                             + rng.uniform(-_COLOR_JITTER, _COLOR_JITTER))), 3)
                             for c in col]
        m = node.get("motion")
        if isinstance(m, dict) and "speed" in m:
            m["speed"] = round(float(m["speed"]) * rng.uniform(*_SPEED_JITTER), 3)
        out.append(node)
    return out


def expand_spec(spec, target: int, seed: int = 0) -> tuple[int, int]:
    """Grow every `world` layer of `spec` in place. Returns (before, after).

    Only `world` layers are touched: it is the only generator whose geometry
    is a list of placements. A `pattern2d` layer's size is its stroke count
    after symmetry expansion, which is a different quantity reached a
    different way.
    """
    before = after = 0
    for layer in getattr(spec, "layers", []) or []:
        params = getattr(layer, "params", None)
        if not isinstance(params, dict) or getattr(layer, "generator", "") != "world":
            continue
        nodes = params.get("nodes") or []
        if not nodes:
            continue
        before += len(nodes)
        grown = expand_nodes(nodes, target,
                             waypoints=(spec.camera or {}).get("waypoints"),
                             seed=seed)
        params["nodes"] = grown
        after += len(grown)
    return before, after
