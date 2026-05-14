"""Small parallel execution helpers for bounded adapter debates."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar


T = TypeVar("T")


def invoke_parallel(tasks: Sequence[Callable[[], T]]) -> list[T]:
    """Run callables concurrently and return results in task order."""

    if not tasks:
        return []
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = [executor.submit(task) for task in tasks]
        results: list[T] = []
        errors: list[Exception] = []
        for future in futures:
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append(exc)
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise RuntimeError(f"{len(errors)} parallel tasks failed: " + "; ".join(str(err) for err in errors))
        return results
