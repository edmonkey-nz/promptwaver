"""Importing this package registers every built-in generator."""

from .base import Generator, Generator3D, create, available, register  # noqa: F401
from . import flow_field, attractor, ripples  # noqa: F401  (self-registering)
from . import ground, forest, world  # noqa: F401  (3D, self-registering)
