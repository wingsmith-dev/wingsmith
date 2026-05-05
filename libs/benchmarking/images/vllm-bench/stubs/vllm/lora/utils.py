import os


def get_adapter_absolute_path(lora_path: str) -> str:
    return os.path.abspath(os.path.expanduser(lora_path))
