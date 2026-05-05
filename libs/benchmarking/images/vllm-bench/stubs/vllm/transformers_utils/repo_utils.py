from functools import cache


@cache
def hf_api():
    from huggingface_hub import HfApi

    return HfApi()
