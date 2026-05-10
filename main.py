"""Thin entrypoint shim — delegates to script_report.__main__.

Usage:
    python main.py build
    python main.py refresh --build-only
    ...

For the full CLI surface, see `python -m script_report --help`.
"""

from script_report.__main__ import main


if __name__ == "__main__":
    main()
