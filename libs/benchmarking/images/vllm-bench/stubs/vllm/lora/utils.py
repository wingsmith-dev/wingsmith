# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# A reduced stand-in for vLLM v0.28.0's vllm/lora/utils.py, without its torch-dependent internals; see README.md.
import os


def get_adapter_absolute_path(lora_path: str) -> str:
    return os.path.abspath(os.path.expanduser(lora_path))
