# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# A reduced stand-in for vLLM v0.28.0's vllm/lora/request.py, without its torch-dependent internals; see README.md.
from dataclasses import dataclass


@dataclass
class LoRARequest:
    lora_name: str
    lora_int_id: int
    lora_path: str = ""
