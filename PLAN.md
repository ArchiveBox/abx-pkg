# Plan: Simplify abx-pkg Library

## Analysis

After reading the full codebase, here are the key complexity problems and proposed solutions:

---

## 1. Eliminate `ShallowBinary` — merge into `Binary` with optional fields

**Problem**: `ShallowBinary` exists as a separate class solely to be a return type from `BinProvider` methods. It's defined inside `binprovider.py` due to circular imports, has a fake re-export module (`shallowbinary.py`), and forces awkward `model_dump()` + reconstruct patterns in `Binary.load()`, `Binary.install()`, etc.

**Solution**: Remove `ShallowBinary` entirely. Make `Binary` the single return type with all "loaded_*" fields optional (which they already are in `Binary`). `BinProvider.load()`, `.install()` etc. return `Binary` directly. This eliminates the circular import, the shallowbinary.py shim, and the repeated `self.__class__(**{**self.model_dump(), **installed_bin.model_dump(...)})` pattern.

**Files changed**: `binprovider.py`, `binary.py`, `shallowbinary.py` (delete), `__init__.py`

---

## 2. Collapse the handler/override system into simple method overriding

**Problem**: The override system (`DEFAULT_OVERRIDES`, `_get_handler_for_action`, `_call_handler_for_action`, `_get_compatible_kwargs`, `_get_handler_keys`) is ~120 lines of indirection. It supports:
- Callable overrides
- String references like `'self.default_install_handler'`
- Literal values wrapped in lambdas
- Runtime `inspect.signature()` introspection to filter kwargs
- A `remap_kwargs` decorator for `packages` → `install_args` aliasing

This is the single biggest source of complexity. Most Python users would expect to just subclass and override methods.

**Solution**:
- Remove the `DEFAULT_OVERRIDES` dict and the `_get_handler_for_action` / `_call_handler_for_action` machinery
- Have `get_abspath()`, `get_version()`, `get_install_args()`, `install()`, etc. call `self.default_*_handler()` directly
- Per-binary overrides (the `Binary.overrides` dict) should be simplified to just `Dict[BinProviderName, Dict[str, list[str]]]` — essentially just `install_args` overrides, which is 95% of actual usage
- Remove `remap_kwargs` decorator — just accept `install_args` as the parameter name
- Remove `_get_compatible_kwargs` (runtime signature introspection)
- Keep `BinProviderOverrides` on `BinProvider` for per-binary `install_args` customization, but as a simple `Dict[BinName, list[str]]` (package name → install args mapping)

**Files changed**: `binprovider.py`, `binary.py`, all `binprovider_*.py` files

---

## 3. DRY up the 5 nearly-identical methods in `Binary`

**Problem**: `Binary.install()`, `load()`, `load_or_install()`, `update()`, `uninstall()` are each ~30 lines and are 90% identical (loop through providers, try each one, collect errors, raise aggregate exception). This is ~150 lines of copy-paste.

**Solution**: Extract a private `_try_providers(action, ...)` method that handles the provider iteration loop. Each public method becomes a 3-5 line wrapper.

**Files changed**: `binary.py`

---

## 4. Replace custom `@binprovider_cache` with `functools.lru_cache` or simple dict caching

**Problem**: Custom caching decorator with `NEVER_CACHE` sentinel values, manual `self._cache` dict management, and `nocache` parameter threading.

**Solution**: Keep simple dict-based caching but inline it into a cleaner pattern. The custom decorator is fine conceptually but can be simplified — remove `NEVER_CACHE` sentinels (just check for `None`), remove the `_cache` field from BinProvider model definition.

**Files changed**: `binprovider.py`

---

## 5. Reduce type alias explosion in the public API

**Problem**: The library exports 8 handler/override type aliases (`BinProviderOverrides`, `BinaryOverrides`, `ProviderFuncReturnValue`, `HandlerType`, `HandlerValue`, `HandlerDict`, `HandlerReturnValue`). These are internal implementation details, not useful to consumers.

**Solution**: After simplifying the handler system (#2), most of these types disappear naturally. Keep only `BinName`, `InstallArgs`, `PATHStr`, `HostBinPath`, `BinProviderName`, `SemVer` as public types. Remove the rest from `__init__.py` exports.

**Files changed**: `__init__.py`, `binprovider.py`

---

## 6. Remove dead/unnecessary code

- `shallowbinary.py` (after #1)
- `get_provider_with_overrides()` method (after #2, overrides are simpler)
- `SelfMethodName` type
- `func_takes_args_or_kwargs()` helper (only used in handler dispatch)
- Commented-out `print()` statements throughout
- `UNKNOWN_ABSPATH` / `UNKNOWN_VERSION` sentinels (only used for dry-run fake results)
- `__package__` declarations at top of every file (not needed)

**Files changed**: various

---

## 7. Simplify `BinProvider.exec()` privilege dropping

**Problem**: Every subprocess call goes through `exec()` which does UID/GID detection, `pwd.getpwuid()` lookups, and `os.setuid()`/`os.setgid()` in `preexec_fn`. This is necessary for root → non-root scenarios but adds complexity for the common case.

**Solution**: Keep the functionality but extract it into a small `_get_subprocess_kwargs()` method. Only apply privilege dropping when `EUID != os.geteuid()`. This makes `exec()` cleaner without losing capability.

**Files changed**: `binprovider.py`

---

## Implementation Order

1. **#2** (handler system) — biggest impact, unblocks other changes
2. **#1** (merge ShallowBinary) — second biggest simplification
3. **#3** (DRY Binary methods) — quick win after #1 and #2
4. **#5** (reduce public API surface) — cleanup
5. **#4** (simplify caching) — minor cleanup
6. **#6** (dead code removal) — final sweep
7. **#7** (exec cleanup) — optional polish

## Estimated Impact

- **Lines removed**: ~300-400 lines of handler dispatch, duplicate methods, and shims
- **Types removed from public API**: 6-7 internal type aliases
- **Files removed**: 1 (`shallowbinary.py`)
- **Conceptual overhead removed**: string-based method references, runtime signature introspection, handler resolution chain, ShallowBinary ↔ Binary conversion
