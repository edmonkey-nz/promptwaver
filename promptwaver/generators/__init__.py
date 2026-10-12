"""Importing this package registers every built-in generator."""

from .base import (  # noqa: F401
    Generator, Generator3D, create, available, register,
    catalog, describe, get, kind_of, MissingGenerator,
)
from . import pattern2d  # noqa: F401  (2D, self-registering)
from . import world  # noqa: F401  (3D, self-registering)
