"""Collection rules shared by the standalone agent-chat test suite."""

import inspect

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makeitem(collector, name, obj):
    """Do not collect helper classes imported from production modules."""

    if inspect.isclass(obj):
        collection_module = getattr(getattr(collector, "obj", None), "__name__", None)
        if collection_module and getattr(obj, "__module__", None) != collection_module:
            return []
    return None
