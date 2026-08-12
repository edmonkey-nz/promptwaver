"""Generator base class and a name registry.

A Generator turns (time, params) into a Frame (list of Paths). Params come
from the scene spec and may be live-modulated by the matrix before render.
Add a new scene type = add a new Generator subclass and @register it. That is
the whole extension story for visuals.

The registry is SELF-DESCRIBING: every generator declares what it is (`kind`,
`description`) and what knobs it has (`defaults` + `param_meta`), and
`catalog()` hands that to whoever needs it. This exists because the two places
that should have been reading the registry were instead hardcoding names —
the director's prompt hardcoded `"generator":"world"`, and the web UI
hardcoded three param keys (`layer0.speed/turbulence/hue`). The result was
that `ripples` and `attractor` could not be reached at all and `flow_field`
was only half-adjustable. Read `catalog()` rather than adding a fourth place
that knows generator names.

`param_meta` gives explicit (min, max[, step]) ranges. Anything omitted is
inferred from the default's type and magnitude, so a new generator gets a
usable panel before anyone writes the metadata — but inference cannot know
that e.g. `step_len` wants a finer step than `turbulence`, so declaring it is
what makes a control feel right.
"""

from __future__ import annotations

from ..geometry import Frame

_REGISTRY: dict[str, type["Generator"]] = {}


def register(name: str):
    def deco(cls):
        cls.name = name
        _REGISTRY[name] = cls
        return cls
    return deco


def create(name: str, **params) -> "Generator":
    if name not in _REGISTRY:
        raise KeyError(f"unknown generator {name!r}; have {sorted(_REGISTRY)}")
    return _REGISTRY[name](**params)


def available() -> list[str]:
    return sorted(_REGISTRY)


def get(name: str) -> type["Generator"] | None:
    return _REGISTRY.get(name)


def describe(name: str) -> dict | None:
    """Schema for one generator, or None if it isn't registered."""
    cls = _REGISTRY.get(name)
    return cls.schema() if cls is not None else None


_catalog_cache: dict[tuple, list[dict]] = {}


def catalog(kind: str | None = None, authorable_only: bool = False) -> list[dict]:
    """Every registered generator's schema, optionally filtered.

    `kind` is "2d" or "3d"; `authorable_only` drops generators that exist for
    backward compatibility but shouldn't be offered for new scenes.

    Memoized: this goes out with every ~20Hz state broadcast, and the answer
    is fixed once the generator modules have imported. Keyed on registry size
    so a late `@register` (a plugin, a test) still invalidates it.
    """
    key = (kind, authorable_only, len(_REGISTRY))
    hit = _catalog_cache.get(key)
    if hit is None:
        out = []
        for cls in _REGISTRY.values():
            if kind is not None and cls.kind() != kind:
                continue
            if authorable_only and not cls.authorable:
                continue
            out.append(cls.schema())
        hit = _catalog_cache[key] = sorted(out, key=lambda s: (s["kind"], s["name"]))
    return hit


def kind_of(generator_names) -> str:
    """The scene kind implied by a set of layer generator names.

    "3d" if ANY layer is 3D — matching Scene.is_3d, which is the single source
    of truth. Unknown names (a scene saved by a newer build, or a generator
    since removed) don't force a guess: they're ignored, and a scene of
    nothing but unknowns reports "2d", which is the safe default because a 2D
    scene simply has no camera rather than a broken one.
    """
    for n in generator_names:
        cls = _REGISTRY.get(n)
        if cls is not None and cls.is_3d:
            return "3d"
    return "2d"


def _infer_range(default) -> dict:
    """A usable slider for a param with no declared range.

    Deliberately conservative: 0..1 for the many normalized floats, and a
    generous 0..4x for counts, which reads better than guessing tight bounds
    and clipping a value the scene already uses.
    """
    if isinstance(default, bool):
        return {"type": "bool"}
    if isinstance(default, int):
        return {"type": "int", "min": 0, "max": max(8, default * 4), "step": 1}
    v = float(default)
    if 0.0 <= v <= 1.0:
        return {"type": "float", "min": 0.0, "max": 1.0, "step": 0.01}
    # signed or >1: a symmetric window around the magnitude, so a coefficient
    # like de Jong's b=-2.3 gets a range it can actually swing across
    m = abs(v) * 2.5 or 1.0
    return {"type": "float", "min": round(-m, 3) if v < 0 else 0.0,
            "max": round(m, 3), "step": 0.01}


class Generator:
    """Subclasses set `defaults` and implement `render`."""

    name = "base"
    description = ""
    defaults: dict = {}
    # key -> (min, max) or (min, max, step). Overrides inference; see module docstring.
    param_meta: dict = {}
    # False = still loadable, but not offered when authoring a new scene.
    authorable = True

    def __init__(self, **params):
        self.params = {**self.defaults, **params}

    is_3d = False

    @classmethod
    def kind(cls) -> str:
        return "3d" if cls.is_3d else "2d"

    @classmethod
    def schema(cls) -> dict:
        """Name, kind, description, and the adjustable scalar params.

        Only int/float/bool defaults become params — a generator whose spec is
        authored data rather than knobs (`world`, with its `defs`/`nodes`)
        correctly reports none, and the UI shows it no slider panel.
        """
        params = []
        for key, default in cls.defaults.items():
            if not isinstance(default, (int, float, bool)):
                continue
            meta = _infer_range(default)
            declared = cls.param_meta.get(key)
            if declared is not None:
                meta["min"], meta["max"] = float(declared[0]), float(declared[1])
                if len(declared) > 2:
                    meta["step"] = float(declared[2])
                if meta["type"] == "int":
                    meta["min"], meta["max"] = int(meta["min"]), int(meta["max"])
                    meta["step"] = max(1, int(meta["step"]))
            params.append({"key": key, "default": default, **meta})
        return {
            "name": cls.name,
            "kind": cls.kind(),
            "description": cls.description,
            "authorable": cls.authorable,
            "params": params,
        }

    @classmethod
    def coerce(cls, key: str, value):
        """Cast an incoming UI/MIDI value to the type this param is declared as.

        The control surface sends everything as a number. Without this an int
        param like `segments` or `rings` lands as a float and the generator's
        own `int()` truncation silently makes the slider feel like it skips —
        and a bool param would become 0.0/1.0.
        """
        d = cls.defaults.get(key)
        if isinstance(d, bool):
            return bool(value)
        if isinstance(d, int):
            return int(round(float(value)))
        return float(value)

    def render(self, t: float, p: dict) -> Frame:  # pragma: no cover
        """Return a Frame for time `t`. `p` is the *resolved* params dict
        (defaults + spec + live modulation) supplied by the engine."""
        raise NotImplementedError


class Generator3D(Generator):
    """A world-space generator. Emits `Path3D`s that the scene's camera projects.
    `field_depth` is the Z wrap length that makes the environment feel endless."""

    is_3d = True
    field_depth = 16.0

    def render3d(self, t: float, p: dict):  # pragma: no cover
        """Return a list[Path3D] for time `t`."""
        raise NotImplementedError
