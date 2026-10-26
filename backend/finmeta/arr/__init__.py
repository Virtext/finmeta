"""Read-only Sonarr/Radarr clients (S9f1a).

Both clients are strictly read-only: they only ever issue ``GET`` requests and
never expose a method that would send POST/PUT/DELETE. A test in
``tests/test_arr.py`` asserts this by inspecting the class API surface.
"""
from finmeta.arr.base import ArrClient, ArrError

__all__ = ["ArrClient", "ArrError"]
