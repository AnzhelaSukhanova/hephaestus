import sys
from typing import Tuple


def call_stack_names(skip: int = 0, limit: int | None = None) -> Tuple[str, ...]:
    frame = sys._getframe(1 + skip)
    names = []
    while frame is not None and (limit is None or len(names) < limit):
        names.append(frame.f_code.co_name)
        frame = frame.f_back
    names.reverse()
    return tuple(names)


def call_stack_tail(limit: int = 4, skip: int = 0) -> Tuple[str, ...]:
    stack = call_stack_names(skip=1 + skip)
    return stack[-limit:]


def called_by_suffix(*names: str, skip: int = 0) -> bool:
    stack = call_stack_names(skip=1 + skip)
    if len(stack) < len(names):
        return False
    return stack[-len(names):] == tuple(names)


def called_under_chain(*names: str, skip: int = 0) -> bool:
    stack = call_stack_names(skip=1 + skip)
    if len(stack) < len(names):
        return False

    for i in range(len(stack) - len(names) + 1):
        if stack[i:i + len(names)] == tuple(names):
            return True
    return False
