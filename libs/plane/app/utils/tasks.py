import asyncio


def cancelled() -> bool:
    """Whether the running task has been asked to stop, even if something on the way swallowed it.

    A database driver interrupted mid-connect can raise its own error in place of the
    `CancelledError`, and a loop that logs and retries every `Exception` then outlives shutdown.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
