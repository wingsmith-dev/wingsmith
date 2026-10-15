# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# A reduced stand-in for vLLM v0.28.0's vllm/utils/collection_utils.py, without its torch-dependent internals; see README.md.
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import TypeVar

_K = TypeVar("_K")
_V = TypeVar("_V")


def full_groupby(values: Iterable[_V], *, key: Callable[[_V], _K]):
    """
    Unlike `itertools.groupby`, groups are not broken by non-contiguous data.
    """
    groups = defaultdict[_K, list[_V]](list)

    for value in values:
        groups[key(value)].append(value)

    return groups.items()
