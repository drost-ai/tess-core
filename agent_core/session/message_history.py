from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


_DirtyCallback = Callable[[], None]


@dataclass(frozen=True)
class SessionPersistenceCheckpoint:
    revision: int
    messageCount: int
    bodyCommitId: str | None


class _TrackedDict(dict):
    def __init__(self, values: Mapping[str, Any], dirty: _DirtyCallback):
        self._dirty = dirty
        dict.__init__(self, values)

    def _materialize(self, key: Any) -> Any:
        value = dict.__getitem__(self, key)
        if isinstance(value, (dict, list)) and not isinstance(
            value, (_TrackedDict, _TrackedList)
        ):
            value = _track_nested(value, self._dirty)
            dict.__setitem__(self, key, value)
        return value

    def __getitem__(self, key: Any) -> Any:
        return self._materialize(key)

    def get(self, key: Any, default: Any = None) -> Any:
        return self._materialize(key) if key in self else default

    def items(self):
        for key in dict.__iter__(self):
            yield key, self._materialize(key)

    def values(self):
        for key in dict.__iter__(self):
            yield self._materialize(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._dirty()
        dict.__setitem__(self, key, _track_nested(value, self._dirty))

    def __delitem__(self, key: Any) -> None:
        self._dirty()
        dict.__delitem__(self, key)

    def clear(self) -> None:
        if self:
            self._dirty()
        dict.clear(self)

    def pop(self, key: Any, *fallback: Any) -> Any:
        if key in self:
            self._dirty()
        return dict.pop(self, key, *fallback)

    def popitem(self) -> tuple[Any, Any]:
        self._dirty()
        return dict.popitem(self)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            self._dirty()
            dict.__setitem__(self, key, _track_nested(default, self._dirty))
        return self._materialize(key)

    def update(self, *args: Any, **kwargs: Any) -> None:
        values = dict(*args, **kwargs)
        if values:
            self._dirty()
        for key, value in values.items():
            dict.__setitem__(self, key, _track_nested(value, self._dirty))

    def __ior__(self, values: Mapping[str, Any]):
        self.update(values)
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        existing = memo.get(id(self))
        if existing is not None:
            return existing
        result: dict[str, Any] = {}
        memo[id(self)] = result
        result.update(
            (copy.deepcopy(key, memo), copy.deepcopy(value, memo))
            for key, value in dict.items(self)
        )
        return result


class _TrackedList(list):
    def __init__(self, values: Iterable[Any], dirty: _DirtyCallback):
        self._dirty = dirty
        list.__init__(self, values)

    def _materialize(self, index: int) -> Any:
        value = list.__getitem__(self, index)
        if isinstance(value, (dict, list)) and not isinstance(
            value, (_TrackedDict, _TrackedList)
        ):
            value = _track_nested(value, self._dirty)
            list.__setitem__(self, index, value)
        return value

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            return [self._materialize(index) for index in range(*key.indices(len(self)))]
        index = int(key)
        if index < 0:
            index += len(self)
        return self._materialize(index)

    def __iter__(self):
        for index in range(len(self)):
            yield self._materialize(index)

    def persistence_values(self):
        """Iterate raw values for read-only sanitization without tracking churn."""
        return list.__iter__(self)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._dirty()
        if isinstance(key, slice):
            list.__setitem__(self, key, [_track_nested(item, self._dirty) for item in value])
        else:
            list.__setitem__(self, key, _track_nested(value, self._dirty))

    def __delitem__(self, key: Any) -> None:
        self._dirty()
        list.__delitem__(self, key)

    def append(self, value: Any) -> None:
        self._dirty()
        list.append(self, _track_nested(value, self._dirty))

    def extend(self, values: Iterable[Any]) -> None:
        items = list(values)
        if items:
            self._dirty()
        list.extend(self, (_track_nested(value, self._dirty) for value in items))

    def insert(self, index: int, value: Any) -> None:
        self._dirty()
        list.insert(self, index, _track_nested(value, self._dirty))

    def pop(self, index: int = -1) -> Any:
        self._dirty()
        return list.pop(self, index)

    def remove(self, value: Any) -> None:
        self._dirty()
        list.remove(self, value)

    def clear(self) -> None:
        if self:
            self._dirty()
        list.clear(self)

    def reverse(self) -> None:
        if len(self) > 1:
            self._dirty()
        list.reverse(self)

    def sort(self, *args: Any, **kwargs: Any) -> None:
        if len(self) > 1:
            self._dirty()
        list.sort(self, *args, **kwargs)

    def __iadd__(self, values: Iterable[Any]):
        self.extend(values)
        return self

    def __imul__(self, count: int):
        if count != 1 and self:
            self._dirty()
        plain = [copy.deepcopy(value) for value in self] * count
        list.__setitem__(self, slice(None), [_track_nested(value, self._dirty) for value in plain])
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        existing = memo.get(id(self))
        if existing is not None:
            return existing
        result: list[Any] = []
        memo[id(self)] = result
        result.extend(copy.deepcopy(value, memo) for value in list.__iter__(self))
        return result


class SessionMessageList(list):
    """List-compatible message history with an in-memory persisted watermark.

    Nested mutations record the earliest affected top-level message. Ordinary
    append-only growth therefore remains eligible for a head/delta commit,
    while edits to an already-persisted prefix fail safely to the full loader.
    The checkpoint is deliberately in-memory only and never enters JSON.
    """

    def __init__(self, values: Iterable[Any] = ()):
        self._checkpoint: SessionPersistenceCheckpoint | None = None
        self._dirty_from = 0
        list.__init__(self)
        self._replace_all(values)

    def checkpoint(self) -> SessionPersistenceCheckpoint | None:
        checkpoint = self._checkpoint
        if (
            checkpoint is None
            or len(self) < checkpoint.messageCount
            or self._dirty_from < checkpoint.messageCount
        ):
            return None
        return checkpoint

    def set_checkpoint(
        self, revision: int, message_count: int, body_commit_id: str | None
    ) -> None:
        if message_count != len(self):
            raise ValueError("Persistence checkpoint must cover the complete live history.")
        self._checkpoint = SessionPersistenceCheckpoint(
            revision, message_count, body_commit_id
        )
        self._dirty_from = message_count

    def invalidate(self, index: int = 0) -> None:
        self._mark_dirty(max(0, index))

    def replace_persisted_canonical(self, values: Iterable[Any]) -> None:
        checkpoint = self._checkpoint
        dirty_from = self._dirty_from
        replacement = list(values)
        if checkpoint is None or len(replacement) != len(self):
            self._mark_dirty(0)
            self._replace_all(replacement)
            return
        self._replace_all(replacement)
        self._checkpoint = checkpoint
        self._dirty_from = dirty_from

    def _mark_dirty(self, index: int) -> None:
        self._dirty_from = min(self._dirty_from, index)

    def _callback(self, index: int) -> _DirtyCallback:
        return lambda: self._mark_dirty(index)

    def _replace_all(self, values: Iterable[Any]) -> None:
        list.clear(self)
        # A slice can contain wrappers owned by a different history. Preserve
        # lazy tracking for plain decoded messages, but detach existing
        # wrappers so their dirty callbacks are rebound on first access here.
        list.extend(
            self,
            [
                copy.deepcopy(value)
                if isinstance(value, (_TrackedDict, _TrackedList))
                else value
                for value in values
            ],
        )

    def _materialize(self, index: int) -> Any:
        value = list.__getitem__(self, index)
        if isinstance(value, dict) and not isinstance(value, _TrackedDict):
            value = _TrackedDict(value, self._callback(index))
            list.__setitem__(self, index, value)
        return value

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            return [self._materialize(index) for index in range(*key.indices(len(self)))]
        index = int(key)
        if index < 0:
            index += len(self)
        return self._materialize(index)

    def __iter__(self):
        for index in range(len(self)):
            yield self._materialize(index)

    def persistence_values(self):
        """Iterate raw values for read-only sanitization without tracking churn."""
        return list.__iter__(self)

    def _invalidate_structure(self, index: int = 0) -> None:
        self._mark_dirty(index)
        # Existing nested callbacks carry their original append-only indices.
        # After structural edits, conservatively rebind every value.
        values = [copy.deepcopy(value) for value in list.__iter__(self)]
        self._replace_all(values)

    def __setitem__(self, key: Any, value: Any) -> None:
        if isinstance(key, slice):
            start = 0 if key.start is None else max(0, int(key.start))
            items = list(value)
            list.__setitem__(self, key, items)
            self._invalidate_structure(start)
            return
        index = int(key)
        if index < 0:
            index = max(0, len(self) + index)
        list.__setitem__(self, key, value)
        self._invalidate_structure(index)

    def __delitem__(self, key: Any) -> None:
        start = 0 if isinstance(key, slice) and key.start is None else int(key.start if isinstance(key, slice) else key)
        list.__delitem__(self, key)
        self._invalidate_structure(max(0, start))

    def append(self, value: Any) -> None:
        index = len(self)
        self._mark_dirty(index)
        list.append(self, _track_nested(value, self._callback(index)))

    def extend(self, values: Iterable[Any]) -> None:
        for value in values:
            self.append(value)

    def insert(self, index: int, value: Any) -> None:
        list.insert(self, index, value)
        self._invalidate_structure(max(0, index))

    def pop(self, index: int = -1) -> Any:
        resolved = len(self) - 1 if index == -1 else max(0, index)
        value = list.pop(self, index)
        self._invalidate_structure(resolved)
        return value

    def remove(self, value: Any) -> None:
        index = self.index(value)
        list.remove(self, value)
        self._invalidate_structure(index)

    def clear(self) -> None:
        if self:
            list.clear(self)
            self._invalidate_structure(0)

    def reverse(self) -> None:
        list.reverse(self)
        self._invalidate_structure(0)

    def sort(self, *args: Any, **kwargs: Any) -> None:
        list.sort(self, *args, **kwargs)
        self._invalidate_structure(0)

    def __iadd__(self, values: Iterable[Any]):
        self.extend(values)
        return self

    def __imul__(self, count: int):
        values = [copy.deepcopy(value) for value in self] * count
        list.__setitem__(self, slice(None), values)
        self._invalidate_structure(0)
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        existing = memo.get(id(self))
        if existing is not None:
            return existing
        result: list[Any] = []
        memo[id(self)] = result
        result.extend(copy.deepcopy(value, memo) for value in list.__iter__(self))
        return result


def createSessionMessageList(values: Iterable[Any] = ()) -> SessionMessageList:
    return SessionMessageList(values)


def ensureSessionMessageTracking(session: dict[str, Any]) -> SessionMessageList:
    messages = session.get("messages")
    if isinstance(messages, SessionMessageList):
        return messages
    tracked = SessionMessageList(messages if isinstance(messages, list) else [])
    session["messages"] = tracked
    return tracked


def sessionPersistenceCheckpoint(session: dict[str, Any]) -> SessionPersistenceCheckpoint | None:
    messages = session.get("messages")
    return messages.checkpoint() if isinstance(messages, SessionMessageList) else None


def setSessionPersistenceCheckpoint(
    session: dict[str, Any],
    revision: int,
    message_count: int | None = None,
    body_commit_id: str | None = None,
) -> None:
    messages = ensureSessionMessageTracking(session)
    messages.set_checkpoint(
        revision,
        len(messages) if message_count is None else message_count,
        body_commit_id,
    )


def invalidateSessionMessagePrefix(session: dict[str, Any], index: int = 0) -> None:
    ensureSessionMessageTracking(session).invalidate(index)


def replaceSessionMessagesWithPersistedCanonical(
    session: dict[str, Any], values: Iterable[Any]
) -> None:
    ensureSessionMessageTracking(session).replace_persisted_canonical(values)


def _track_nested(value: Any, dirty: _DirtyCallback) -> Any:
    if isinstance(value, dict):
        return _TrackedDict(value, dirty)
    if isinstance(value, list):
        return _TrackedList(value, dirty)
    return value


__all__ = [
    "SessionMessageList",
    "SessionPersistenceCheckpoint",
    "createSessionMessageList",
    "ensureSessionMessageTracking",
    "invalidateSessionMessagePrefix",
    "replaceSessionMessagesWithPersistedCanonical",
    "sessionPersistenceCheckpoint",
    "setSessionPersistenceCheckpoint",
]
