#!/usr/bin/env python3
"""Red-team harness entry point -- see rt_harness/ for the implementation.

Equivalent invocations:

    ./thinlizzy.py
    python3 -m rt_harness

Environment variables are unchanged from the shell harness; run --help for the
flags that override them.
"""

from __future__ import annotations

import sys

from rt_harness.cli import main

if __name__ == "__main__":
    sys.exit(main())
