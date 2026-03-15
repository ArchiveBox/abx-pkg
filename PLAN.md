# Plan: Lazy provider singletons

## Goal
Allow `from abx_pkg import apt, pip, brew` to return lazy-instantiated provider singletons,
so users don't need to manually call `AptProvider()` etc.

## Change

### `__getattr__` lazy provider singletons in `__init__.py`

```python
from abx_pkg import apt, pip, brew  # lazy, instantiated on first access
apt.install("curl")                  # no manual AptProvider() needed
```

Provider classes (`AptProvider`, etc.) remain importable for custom instantiation:
```python
from abx_pkg import PipProvider
pip = PipProvider(pip_venv=Path("/my/venv"))  # still works
```

Implementation: `__getattr__` + `_provider_singletons` cache dict in `__init__.py`.
Singletons are created on first attribute access and cached for subsequent uses.

## What does NOT change

Everything else — Binary, ShallowBinary, BinProvider, override system, etc.
