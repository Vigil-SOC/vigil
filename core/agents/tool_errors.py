"""Errors a backend tool raises for tools_router to classify.

Kept free of FastAPI so memory and registry code can raise them.
"""


class InvalidToolArgs(TypeError):
    """A call the caller can fix. tools_router answers it as invalid_args."""
