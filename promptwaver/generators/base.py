"""Generator base class and a name registry.

A Generator turns (time, params) into a Frame (list of Paths). Params come
from the scene spec and may be live-modulated by the matrix before render.
Add a new scene type = add a new Generator subclass and @register it. That is
the whole extension story for visuals.
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


class Generator:
    """Subclasses set `defaults` and implement `render`."""

    name = "base"
    defaults: dict = {}

    def __init__(self, **params):
        self.params = {**self.defaults, **params}

    is_3d = False

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
