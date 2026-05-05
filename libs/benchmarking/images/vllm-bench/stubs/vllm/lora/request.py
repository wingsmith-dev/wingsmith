from dataclasses import dataclass


@dataclass
class LoRARequest:
    lora_name: str
    lora_int_id: int
    lora_path: str = ""
