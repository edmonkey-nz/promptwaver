"""Importing this package registers every built-in generator."""

from .base import (  # noqa: F401
    Generator, Generator3D, create, available, register,
    catalog, describe, get, kind_of,
)
from . import flow_field, attractor, ripples, pattern2d  # noqa: F401  (self-registering)
from . import ground, forest, world  # noqa: F401  (3D, self-registering)
