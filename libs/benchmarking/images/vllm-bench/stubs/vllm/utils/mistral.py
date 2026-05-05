def is_mistral_tokenizer(tokenizer) -> bool:
    return type(tokenizer).__name__ == "MistralTokenizer"
