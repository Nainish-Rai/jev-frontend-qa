"""Jev Frontend QA package.

The public API surface for downstream tickets is concentrated in
:mod:`jev_frontend_qa.core.models` (Pydantic schema), :mod:`.core.agent`
(:class:`Runner`, :func:`interpolate_fixtures`), :mod:`.core.decisions`
(:class:`DecisionProvider` seam), and :mod:`.core.policy`
(:class:`PolicyEnforcer`). The CLI is the canonical entry point; tests
import from the core modules directly.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
