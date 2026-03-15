# Plan: Simplify abx-pkg Library

## Design Constraint: The Override System is Load-Bearing

The override system supports a key architectural requirement: overrides can be defined on **both** `Binary` (keyed by provider name) **and** `BinProvider` (keyed by binary name), and they get **merged at call time** when `Binary.get_binprovider()` calls `provider.get_provider_with_overrides()`. This two-sided merge cannot be replaced by simple class inheritance because:

1. A `Binary('yt-dlp')` needs to say "when using pip, install `yt-dlp[default,curl-cffi]`" — that's per-binary, per-provider
2. A `BinProvider` needs to say "for python, the abspath is always `sys.executable`" — that's per-provider, per-binary
3. Overrides can be callables, literal values, or string references to methods — all handler types (abspath, version, install_args, install, update, uninstall) need to be overridable, not just install_args

The handler dispatch (`_get_handler_for_action` → `_call_handler_for_action`) is the mechanism that resolves these merged overrides at runtime. **It stays.**

---

## Viable Simplifications

### 1. DRY up the 5 nearly-identical methods in `Binary`

**Problem**: `Binary.install()`, `load()`, `load_or_install()`, `update()`, `uninstall()` are each ~30 lines and are 90% identical (loop through providers, try each one, collect errors, raise aggregate exception). This is ~150 lines of copy-paste.

**Solution**: Extract a private `_try_providers(action_name, ...)` method that handles the provider iteration loop. Each public method becomes a 3-5 line wrapper calling `_try_providers('install')`, `_try_providers('load')`, etc. The `uninstall` variant resets loaded fields instead of merging them.

**Files changed**: `binary.py`

---

### 2. Eliminate `ShallowBinary` — merge into `Binary` with optional fields

**Problem**: `ShallowBinary` exists as a separate class solely to be a return type from `BinProvider` methods. It's defined inside `binprovider.py` due to circular imports, has a fake re-export shim (`shallowbinary.py` is literally 3 lines that re-import from `binprovider.py`), and forces awkward `model_dump()` → reconstruct patterns in every `Binary` method.

**Solution**: Remove `ShallowBinary`. Move the computed properties (`bin_filename`, `is_executable`, `is_script`, `is_valid`, `bin_dir`, `loaded_respath`, `exec()`) into `Binary` directly (they're already duplicated/overridden there anyway). `BinProvider.load()`, `.install()` etc. return a plain dict or a lightweight dataclass/NamedTuple with `(name, abspath, version, sha256)` — just the data, no model. `Binary` methods consume these dicts to construct the returned `Binary`.

This avoids the circular import entirely: `BinProvider` no longer needs to reference any Binary type, it just returns a dict of results.

**Files changed**: `binprovider.py`, `binary.py`, `shallowbinary.py` (delete), `__init__.py`

---

### 3. Reduce the type alias explosion at the bottom of `binprovider.py`

**Problem**: Lines 965-1031 define ~30 type aliases (6 return value types, 6 Protocol classes, 6 NoArgs callable types, 6 HandlerValue unions, plus HandlerType, HandlerValue, HandlerReturnValue, HandlerDict, BinaryOverrides, BinProviderOverrides). Most are not used for runtime validation — they're purely for documentation/type-checking.

**Solution**: Collapse into fewer aliases:
- Keep `HandlerType`, `HandlerDict`, `BinaryOverrides`, `BinProviderOverrides` (these are the public-facing types users interact with)
- Collapse the 6 Protocol + 6 NoArgs + 6 HandlerValue groups into a single `HandlerValue = Callable[..., Any] | str | list[str] | SemVer | Path | bool | None`
- Remove the per-action return value types (the handler dispatch already uses `cast()` so precise return types don't provide runtime value)
- Stop exporting internal types from `__init__.py` — only export `BinaryOverrides`, `BinProviderOverrides`, `HandlerDict` since users need those to define overrides

**Files changed**: `binprovider.py`, `__init__.py`

---

### 4. Simplify `_call_handler_for_action` kwargs handling

**Problem**: `_get_compatible_kwargs()` does `inspect.signature()` introspection on every handler call to filter kwargs to only those the handler accepts. `func_takes_args_or_kwargs()` inspects bytecode flags. This is defensive programming against handler signature mismatches.

**Solution**: Just pass `**kwargs` and let handlers use `**context` (which they already all do). Remove `_get_compatible_kwargs` and `func_takes_args_or_kwargs`. If a handler doesn't want extra kwargs, it should declare `**context` or `**kwargs` — which all existing handlers already do. For the no-args lambda case (`lambda: ['wget']`), wrap in a try/except TypeError instead of bytecode introspection.

**Files changed**: `binprovider.py`, `base_types.py`

---

### 5. Remove `remap_kwargs` decorator

**Problem**: `@remap_kwargs({'packages': 'install_args'})` is applied to nearly every handler method to support accepting either `packages=` or `install_args=` as a kwarg name. This adds a decorator layer to every method.

**Solution**: Pick one name (`install_args`) and use it everywhere. Add `packages` as a simple alias only in the handler dispatch — when calling a handler, if `install_args` is in kwargs, also pass it as `packages` (or vice versa). One line in `_call_handler_for_action` replaces 15+ decorator applications.

**Files changed**: `binprovider.py`, all `binprovider_*.py` files

---

### 6. Clean up minor cruft

- Commented-out `print()` and `# signal` blocks throughout `binprovider.py`
- `__package__` declarations at top of every file (not needed, Python sets this automatically)
- `UNKNOWN_ABSPATH` / `UNKNOWN_VERSION` sentinels could be replaced with simpler defaults
- `packages` alias in `_get_handler_keys` (replaced by #5)
- `NEVER_CACHE` tuple can be simplified to just checking `is None`

**Files changed**: various

---

## Implementation Order

1. **#1** (DRY Binary methods) — standalone, low-risk, immediate ~120 line reduction
2. **#2** (eliminate ShallowBinary) — biggest structural simplification
3. **#5** (remove remap_kwargs) — quick win, touches many files but trivially
4. **#4** (simplify kwargs handling) — removes introspection code
5. **#3** (reduce type aliases) — cleanup after #4
6. **#6** (cruft removal) — final sweep

## Estimated Impact

- **Lines removed**: ~200-250 (mostly duplicate Binary methods, ShallowBinary class, type aliases)
- **Files removed**: 1 (`shallowbinary.py`)
- **Decorators removed**: `@remap_kwargs` from ~15 methods
- **Runtime introspection removed**: `inspect.signature()` calls, bytecode flag checking
- **Core override system**: Preserved intact — handler dispatch, two-sided override merging, callable/literal/string-ref support all stay
