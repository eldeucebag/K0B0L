"""Red-team harness for a local Ollama box.

The point of this package is the shape of the loop, not the models in it. Three
roles are wired in a fixed order, each with its own context and output budget,
and every call is instrumented so a silently truncated or silently polluted
generation cannot pass for a clean result.

See :mod:`rt_harness.pipeline` for how to add or reorder stages.
"""

__all__ = ["__version__"]

__version__ = "5.0.0"
