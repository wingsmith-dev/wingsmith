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
