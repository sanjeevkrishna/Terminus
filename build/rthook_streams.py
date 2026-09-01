"""Runtime hook: give stdio a sink in windowed builds.

A GUI build has no console, so sys.stdout/stderr/stdin are None. Any library
that writes to them raises AttributeError, which in a background thread becomes
a silent stall. Same failure mode as running under pythonw.exe.

File path: build/rthook_streams.py
"""

import os
import sys

for _name in ("stdin", "stdout", "stderr"):
    if getattr(sys, _name, None) is None:
        # adds nothing for os.devnull.
        _sink = open(
            os.devnull, "r" if _name == "stdin" else "w", encoding="utf-8"
        )
        setattr(sys, _name, _sink)
        setattr(sys, f"__{_name}__", _sink)
