"""Errors a backend tool raises for tools_router to classify.

Kept free of FastAPI so memory and registry code can raise them.
"""


# A call the caller can fix. tools_router answers it as invalid_args, which tells
# the model to call again. A TypeError so code that catches the old spelling
# still does.
class InvalidToolArgs(TypeError):
    pass
