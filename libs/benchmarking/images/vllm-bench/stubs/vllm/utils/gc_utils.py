import gc


def freeze_gc_heap() -> None:
    gc.collect()
    gc.freeze()
