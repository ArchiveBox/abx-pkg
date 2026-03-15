__package__ = "abx_pkg"

import functools
import logging as py_logging
import shlex

from pathlib import Path
from typing import Any, Callable

from typing_extensions import ParamSpec, TypeVar

LOGGER_NAME = "abx_pkg"
DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

logger = py_logging.getLogger(LOGGER_NAME)
if not any(isinstance(handler, py_logging.NullHandler) for handler in logger.handlers):
    logger.addHandler(py_logging.NullHandler())

P = ParamSpec("P")
R = TypeVar("R")


def get_logger(name: str | None = None) -> py_logging.Logger:
    if not name or name == LOGGER_NAME:
        return logger
    return py_logging.getLogger(name)


def normalize_log_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    normalized = getattr(py_logging, str(level).upper(), None)
    if isinstance(normalized, int):
        return normalized
    raise ValueError(f"Unsupported log level: {level}")


def configure_logging(
    level: int | str = py_logging.INFO,
    handler: py_logging.Handler | None = None,
    fmt: str = DEFAULT_LOG_FORMAT,
    datefmt: str | None = None,
    propagate: bool = False,
    replace_handlers: bool = False,
) -> py_logging.Logger:
    package_logger = get_logger()
    package_logger.setLevel(normalize_log_level(level))
    package_logger.propagate = propagate

    if replace_handlers:
        package_logger.handlers.clear()

    if handler is None:
        handler = py_logging.StreamHandler()

    if handler.formatter is None:
        handler.setFormatter(py_logging.Formatter(fmt=fmt, datefmt=datefmt))

    if handler not in package_logger.handlers:
        package_logger.addHandler(handler)

    return package_logger


def format_command(cmd: list[str] | tuple[str, ...]) -> str:
    return shlex.join(str(part) for part in cmd)


def summarize_value(value: Any, max_length: int = 200) -> str:
    if isinstance(value, Path):
        rendered = str(value)
    elif isinstance(value, (str, int, float, bool, type(None))):
        rendered = repr(value)
    elif isinstance(value, dict):
        items = ", ".join(f"{summarize_value(key, 40)}: {summarize_value(val, 60)}" for key, val in list(value.items())[:4])
        rendered = f"{{{items}}}"
    elif isinstance(value, (list, tuple, set, frozenset)):
        rendered_items = ", ".join(summarize_value(item, 40) for item in list(value)[:4])
        rendered = f"{type(value).__name__}([{rendered_items}])"
    elif hasattr(value, "name"):
        name = getattr(value, "name", None)
        rendered = f"{value.__class__.__name__}(name={name!r})"
    else:
        rendered = repr(value)

    if len(rendered) > max_length:
        return rendered[: max_length - 3] + "..."
    return rendered


def _format_method_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    rendered_args = [summarize_value(arg, 80) for arg in args]
    rendered_kwargs = [f"{key}={summarize_value(value, 80)}" for key, value in kwargs.items()]
    return ", ".join([*rendered_args, *rendered_kwargs])


def log_method_call(level: int = py_logging.DEBUG, include_result: bool = False) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            method_logger = get_logger(func.__module__)
            if method_logger.isEnabledFor(level):
                method_logger.log(level, "Calling %s(%s)", func.__qualname__, _format_method_call(args, kwargs))
            try:
                result = func(*args, **kwargs)
            except Exception:
                if method_logger.isEnabledFor(py_logging.WARNING):
                    method_logger.exception("%s raised an exception", func.__qualname__)
                raise
            if include_result and method_logger.isEnabledFor(level):
                method_logger.log(level, "%s returned %s", func.__qualname__, summarize_value(result))
            return result

        return wrapper

    return decorator


def log_subprocess_error(command_logger: py_logging.Logger, action: str, stdout: str | None, stderr: str | None) -> None:
    trimmed_stdout = (stdout or "").strip()
    trimmed_stderr = (stderr or "").strip()
    if trimmed_stdout:
        command_logger.error("%s stdout: %s", action, trimmed_stdout)
    if trimmed_stderr:
        command_logger.error("%s stderr: %s", action, trimmed_stderr)
