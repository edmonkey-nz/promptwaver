"""Deterministic local keyword -> SceneSpec mapping.

Used when no Anthropic API key is configured, and as the schema example shown
to Claude. Keeps LaserFlow fully usable offline: the API is an *optional*
upgrade to the director, never a hard dependency.
"""

from __future__ import annotations

from ..scenes import SceneSpec, Layer

# keyword substrings -> (generator, params, palette, hue)
_RULES = [
    (("water", "flow", "river", "stream", "ocean", "sea"),
     "flow_field", dict(turbulence=0.3, speed=0.18, hue=0.55),
     ["#0a3d62", "#3c9dd0", "#c8f0ff"]),
    (("cloud", "mist", "fog", "sky", "wind"),
     "flow_field", dict(turbulence=0.15, speed=0.1, hue=0.6, particles=64),
     ["#2c3e50", "#8fa8c0", "#e8eef5"]),
    (("smoke", "drift", "nebula", "swirl", "aurora"),
     "attractor", dict(drift=0.2, speed=0.08, hue=0.72),
     ["#1a1a2e", "#7b6bd9", "#c8bfff"]),
    (("drip", "rain", "pool", "pond", "ripple", "droplet"),
     "ripples", dict(speed=0.22, rings=5, hue=0.58),
     ["#06344a", "#2a8fb0", "#bff0ff"]),
    (("forest", "leaf", "tree", "grove"),
     "attractor", dict(a=1.7, b=-1.8, drift=0.1, speed=0.06, hue=0.33),
     ["#0b3d0b", "#3f8f3f", "#bfe6a0"]),
]

_DEFAULT_PATCH = dict(engine="pad", waveform="triangle", voices=4,
                      attack=3.0, release=6.0, base_note=48, chord=[0, 7, 12, 16])

# keyword groups routed to specific builders
_FOREST_KEYS = ("forest", "trees", "woods", "grove", "jungle")
_GENERIC3D_KEYS = ("float", "fly through", "flythrough", "immerse", "immersive",
                   "3d", "navigate", "drift")
_INTERIOR_KEYS = ("studio", "painter", "painting", "easel", "atelier", "art studio")

_SPACE_KEYS = ("space", "planet", "cosmic", "galaxy", "star", "orbit", "nebula",
               "solar", "saturn", "moon", "asteroid", "void")
_SEA_KEYS = ("jellyfish", "underwater", "ocean", "reef", "coral", "deep", "sea",
             "aquatic", "abyss", "whale", "swim", "dolphin", "kelp", "lagoon")


def _forest_3d(keyword: str) -> SceneSpec:
    return SceneSpec(
        name=keyword.strip()[:40] or "fly-through",
        layers=[
            Layer(generator="ground_grid", params=dict(rails=7, rungs=16)),
            Layer(generator="forest", params=dict(trees=14, spread=5.0)),
        ],
        palette=["#0b3d0b", "#2f8f4f", "#0a1f3a"],
        audio_patch=dict(_DEFAULT_PATCH, base_note=45, chord=[0, 7, 12, 19]),
        # on/off laser: show depth with COLOUR, not brightness (near green ->
        # far deep-blue). Switch mode to "cull" for hard depth culling instead,
        # or set ttl_quantize: true to snap to clean TTL-RGB primaries.
        camera=dict(fov=62, near=0.4, far=14.0, speed=0.6, height=1.0,
                    max_strokes=90,
                    depth=dict(mode="hue", near_color=[0.3, 0.95, 0.5],
                               far_color=[0.08, 0.15, 0.5], ttl_quantize=False)),
        modulation=[
            # audio nudges how fast you drift through the trees
            dict(source="audio_level", dest="camera.speed", depth=0.5, bias=0.0),
            dict(source="lfo_slow", dest="audio.cutoff", depth=400.0),
        ],
    )


def _world(name, nodes, camera, palette, patch=None):
    return SceneSpec(
        name=name.strip()[:40] or "world",
        layers=[Layer(generator="world", params=dict(nodes=nodes))],
        palette=palette,
        audio_patch=patch or dict(_DEFAULT_PATCH, base_note=43, chord=[0, 7, 12, 19]),
        camera=camera,
        modulation=[
            dict(source="audio_level", dest="camera.speed", depth=0.6),
            dict(source="lfo_slow", dest="audio.cutoff", depth=400.0),
        ],
    )


def _space_world(keyword: str) -> SceneSpec:
    nodes = [
        dict(primitive="starfield", pos=[0, 0, 0], scale=1.0, color=[0.7, 0.8, 1.0],
             params=dict(count=36, spread=9.0)),
        dict(primitive="planet", pos=[0, 0, 0], scale=3.0, color=[0.35, 0.6, 1.0],
             params=dict(lat=3, lon=5), motion=dict(type="spin", speed=0.15)),
        dict(primitive="ring", pos=[5.5, 1.0, -3], scale=2.2, color=[0.9, 0.85, 0.6],
             params=dict(tilt=0.5), motion=dict(type="spin", speed=0.1, axis="y")),
        dict(primitive="planet", pos=[5.5, 1.0, -3], scale=1.1, color=[0.95, 0.8, 0.5],
             params=dict(lat=2, lon=4), motion=dict(type="spin", speed=0.2)),
        dict(primitive="ball", pos=[-6, -1.5, 2], scale=0.8, color=[0.8, 0.9, 1.0],
             motion=dict(type="drift", speed=0.3, amp=0.6)),
        dict(primitive="crystal", pos=[-3, 3, -5], scale=0.7, color=[0.7, 1.0, 0.9],
             motion=dict(type="spin", speed=0.5, axis="x")),
    ]
    camera = dict(mode="orbit", target=[0, 0, 0], orbit_radius=10.0, orbit_height=1.5,
                  fov=62, near=0.4, far=30.0, speed=0.6, max_strokes=120,
                  depth=dict(mode="cull"))
    return _world(keyword, nodes, camera, ["#0a0a2a", "#3a5bd0", "#c8b0ff"])


def _underwater_world(keyword: str) -> SceneSpec:
    nodes = [
        dict(primitive="jellyfish", pos=[0, 1, 0], scale=1.4, color=[0.4, 1.0, 0.9],
             params=dict(tentacles=6), motion=dict(type="bob", speed=0.6, amp=0.5)),
        dict(primitive="jellyfish", pos=[4, -1, -3], scale=1.0, color=[0.5, 0.9, 1.0],
             params=dict(tentacles=5), motion=dict(type="bob", speed=0.5, amp=0.6)),
        dict(primitive="jellyfish", pos=[-4, 2, 2], scale=0.8, color=[0.6, 1.0, 0.8],
             params=dict(tentacles=6), motion=dict(type="drift", speed=0.4, amp=0.7)),
        dict(primitive="ball", pos=[2, -2, 1], scale=0.3, color=[0.7, 1.0, 1.0],
             motion=dict(type="bob", speed=1.2, amp=1.0)),
        dict(primitive="ball", pos=[-2, -3, -2], scale=0.2, color=[0.7, 1.0, 1.0],
             motion=dict(type="bob", speed=1.5, amp=1.2)),
        dict(primitive="crystal", pos=[0, -4, 0], scale=1.2, color=[0.3, 0.7, 0.6],
             motion=dict(type="spin", speed=0.1)),
    ]
    camera = dict(mode="drift", target=[0, -0.5, 0], orbit_radius=7.0, orbit_height=0.5,
                  fov=64, near=0.4, far=24.0, speed=0.5, max_strokes=120,
                  depth=dict(mode="cull"))
    return _world(keyword, nodes, camera, ["#04283a", "#2aa0b0", "#bfffe0"],
                  patch=dict(_DEFAULT_PATCH, base_note=40, chord=[0, 5, 12, 17],
                             attack=5.0, release=9.0))


def _studio_world(keyword: str) -> SceneSpec:
    """A hand-authored interior built entirely from scene `defs` — the same
    mechanism Claude uses online. Demonstrates that geometry can live in the
    scene JSON with no app-side primitive for 'easel', 'stool', etc."""
    defs = {
        "floor": [dict(op="grid", w=18, h=18, nx=7, ny=7, plane="xz", c=[0, -2, 0])],
        "easel": [
            dict(op="line", a=[0, 2.4, 0], b=[-0.9, -2, 0.7]),
            dict(op="line", a=[0, 2.4, 0], b=[0.9, -2, 0.7]),
            dict(op="line", a=[0, 2.4, 0], b=[0, -2, -1.1]),
            dict(op="line", a=[-0.9, -0.4, 0.7], b=[0.9, -0.4, 0.7]),
        ],
        "canvas": [
            dict(op="rect", w=1.7, h=2.1, plane="xy", c=[0, 0.5, 0]),
            dict(op="rect", w=1.4, h=1.8, plane="xy", c=[0, 0.5, 0.02]),
        ],
        "stool": [
            dict(op="rect", w=1.0, h=1.0, plane="xz", c=[0, 0, 0]),
            dict(op="line", a=[-0.5, 0, -0.5], b=[-0.5, -1.6, -0.5]),
            dict(op="line", a=[0.5, 0, -0.5], b=[0.5, -1.6, -0.5]),
            dict(op="line", a=[0.5, 0, 0.5], b=[0.5, -1.6, 0.5]),
            dict(op="line", a=[-0.5, 0, 0.5], b=[-0.5, -1.6, 0.5]),
        ],
        "window": [
            dict(op="rect", w=2.4, h=3.0, plane="xy", c=[0, 0, 0]),
            dict(op="line", a=[0, -1.5, 0], b=[0, 1.5, 0]),
            dict(op="line", a=[-1.2, 0, 0], b=[1.2, 0, 0]),
        ],
        "jar": [
            dict(op="lathe", profile=[[0.0, 0], [0.35, 0], [0.4, 0.25],
                                      [0.3, 0.7], [0.34, 0.95]], seg=14, meridians=4),
            dict(op="polyline", pts=[[0.05, 0.9, 0], [0.15, 1.9, 0.05]]),
            dict(op="polyline", pts=[[-0.1, 0.9, 0.05], [-0.2, 2.0, -0.05]]),
            dict(op="polyline", pts=[[0.0, 0.9, -0.1], [0.05, 1.7, -0.2]]),
        ],
        "lamp": [
            dict(op="lathe", profile=[[0.55, 0], [0.12, 0.7]], seg=14, meridians=4),
            dict(op="line", a=[0, 0.7, 0], b=[0, 2.6, 0]),
        ],
    }
    nodes = [
        dict(shape="floor", pos=[0, 0, 0], scale=1.0, color=[0.25, 0.35, 0.5]),
        dict(shape="easel", pos=[0, 0.5, -1], scale=1.2, color=[0.95, 0.8, 0.5]),
        dict(shape="canvas", pos=[0, 1.0, -0.3], scale=1.2, color=[1.0, 0.95, 0.85]),
        dict(shape="stool", pos=[3, -0.4, 1], scale=1.0, color=[0.8, 0.6, 0.4]),
        dict(shape="window", pos=[-5, 1.5, -4], scale=1.3, color=[0.6, 0.85, 1.0]),
        dict(shape="jar", pos=[4.5, 0.2, -2], scale=1.2, color=[0.7, 1.0, 0.9],
             motion=dict(type="none")),
        dict(shape="lamp", pos=[-2, 3.0, 2], scale=1.2, color=[1.0, 0.85, 0.5],
             motion=dict(type="bob", speed=0.4, amp=0.2)),
    ]
    camera = dict(mode="drift", target=[0, 0, -1], orbit_radius=7.0, orbit_height=0.8,
                  fov=66, near=0.4, far=26.0, speed=0.4, max_strokes=130,
                  depth=dict(mode="cull"))
    return SceneSpec(
        name=keyword.strip()[:40] or "studio",
        layers=[Layer(generator="world", params=dict(defs=defs, nodes=nodes))],
        palette=["#141018", "#c8a060", "#e8dcc0"],
        audio_patch=dict(_DEFAULT_PATCH, base_note=46, chord=[0, 4, 7, 11],
                         attack=4.0, release=8.0),
        camera=camera,
        modulation=[
            dict(source="audio_level", dest="camera.speed", depth=0.5),
            dict(source="lfo_slow", dest="audio.cutoff", depth=400.0),
        ],
    )


def local_scene(keyword: str) -> SceneSpec:
    kw = keyword.lower()
    if any(k in kw for k in _SPACE_KEYS):
        return _space_world(keyword)
    if any(k in kw for k in _SEA_KEYS):
        return _underwater_world(keyword)
    if any(k in kw for k in _FOREST_KEYS):
        return _forest_3d(keyword)
    if any(k in kw for k in _INTERIOR_KEYS):
        return _studio_world(keyword)
    if any(k in kw for k in _GENERIC3D_KEYS):
        return _space_world(keyword)     # nicest default immersive scene
    for keys, generator, params, palette in _RULES:
        if any(k in kw for k in keys):
            gen, pr, pal = generator, params, palette
            break
    else:
        gen, pr, pal = "flow_field", dict(hue=0.5), ["#222831", "#4a90a4", "#eeeeee"]

    patch = dict(_DEFAULT_PATCH)
    # slower, calmer envelope for calmer keywords
    if any(w in kw for w in ("calm", "slow", "deep", "still")):
        patch.update(attack=5.0, release=9.0)

    return SceneSpec(
        name=keyword.strip()[:40] or "scene",
        layers=[Layer(generator=gen, params=pr)],
        palette=pal,
        audio_patch=patch,
        modulation=[
            # calm defaults: a slow LFO breathes the visuals + synth filter,
            # and live audio level nudges turbulence (audio -> visual bridge)
            dict(source="lfo_slow", dest="visual.speed", depth=0.05, bias=0.0),
            dict(source="lfo_slow", dest="audio.cutoff", depth=400.0, bias=0.0),
            dict(source="audio_level", dest="visual.turbulence", depth=0.4),
        ],
    )
