"""Run dasungctl from a source checkout that is not installed.

`pyproject.toml` maps the `dasungctl` package onto the `src/` directory, so
the import name does not match a directory in the checkout and a plain
`python -m dasungctl` cannot find the sources. Importing this module
registers a finder that resolves `dasungctl` and its submodules here;
running it as a script starts the module named after `--module`
(`dasungctl.__main__` by default, the command-line entry point). The tray
re-exec and the autostart fallback start it this way.
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import os
import runpy
import sys
from typing import Sequence

PACKAGE = "dasungctl"
HERE = os.path.dirname(os.path.abspath(__file__))


class _SourceFinder(importlib.abc.MetaPathFinder):
    """Resolve the package name onto this source directory."""

    def find_spec(self, name, path=None, target=None):
        if name != PACKAGE and not name.startswith(PACKAGE + "."):
            return None
        parts = name.split(".")[1:]
        base = os.path.join(HERE, *parts)
        module = base + ".py"
        if os.path.isfile(module):
            return importlib.util.spec_from_file_location(name, module)
        if os.path.isdir(base):
            init = os.path.join(base, "__init__.py")
            if os.path.isfile(init):
                return importlib.util.spec_from_file_location(
                    name, init, submodule_search_locations=[base]
                )
        return None


def install() -> None:
    """Make `import dasungctl` work without an installed package."""

    if not any(isinstance(finder, _SourceFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _SourceFinder())


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    target = PACKAGE + ".__main__"
    if args and args[0] == "--module":
        target = args[1]
        del args[:2]
    sys.argv[1:] = args
    install()
    runpy.run_module(target, run_name="__main__", alter_sys=True)
    return 0


install()

if __name__ == "__main__":
    raise SystemExit(main())
