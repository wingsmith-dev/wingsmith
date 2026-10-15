# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# A reduced stand-in for vLLM v0.28.0's vllm/transformers_utils/repo_utils.py, without its torch-dependent internals; see README.md.
from functools import cache


@cache
def hf_api():
    from huggingface_hub import HfApi

    return HfApi()
