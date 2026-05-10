"""Tiny logging shim used by the pipeline scripts.

Most of the pipeline writes to stdout via ``print()``; that's intentional —
the output is meant to be readable in a terminal during long-running batch
jobs. This module exists so callers can opt into a slightly richer formatter
when they want one (e.g. for refresh.py's step banners).
"""

from __future__ import annotations


def banner(title: str, width: int = 60, char: str = "=") -> None:
    """Print a centred banner — used between pipeline stages."""
    print(char * width)
    print(f"  {title}")
    print(char * width)


def step(label: str) -> None:
    """Single-line step indicator: ``[1/5] Loading PBS ATC data...``"""
    print(f"\n{label}")
