# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# A reduced stand-in for vLLM v0.28.0's vllm/tokenizers.py, without its torch-dependent internals; see README.md.
from typing import Any

TokenizerLike = Any


def get_tokenizer(
    tokenizer_name: str,
    *args: Any,
    tokenizer_mode: str = "auto",
    trust_remote_code: bool = False,
    **kwargs: Any,
) -> Any:
    from transformers import AutoTokenizer

    kwargs.pop("revision", None)
    return AutoTokenizer.from_pretrained(
        tokenizer_name,
        trust_remote_code=trust_remote_code,
        use_fast=tokenizer_mode != "slow",
    )
