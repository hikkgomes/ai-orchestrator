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
        return [future.result() for future in futures]
