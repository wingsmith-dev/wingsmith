# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# A reduced stand-in for vLLM v0.28.0's vllm/utils/gc_utils.py, without its torch-dependent internals; see README.md.
import gc


def freeze_gc_heap() -> None:
    gc.collect()
    gc.freeze()
