from typing import Any


class _PlaceholderAttr:
    def __init__(self, name: str) -> None:
        self._name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise ImportError(f"{self._name} is not installed in this slim build")

    def __getattr__(self, item: str) -> Any:
        return _PlaceholderAttr(f"{self._name}.{item}")


class PlaceholderModule:
    def __init__(self, name: str) -> None:
        self._name = name

    def placeholder_attr(self, attr_path: str) -> Any:
        return _PlaceholderAttr(f"{self._name}.{attr_path}")

    def __getattr__(self, item: str) -> Any:
        return _PlaceholderAttr(f"{self._name}.{item}")
