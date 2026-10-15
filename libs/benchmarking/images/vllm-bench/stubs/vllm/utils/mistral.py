# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
# A reduced stand-in for vLLM v0.28.0's vllm/utils/mistral.py, without its torch-dependent internals; see README.md.
def is_mistral_tokenizer(tokenizer) -> bool:
    return type(tokenizer).__name__ == "MistralTokenizer"
