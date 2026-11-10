from pydantic_ai import models

# Agent tests run on TestModel/FunctionModel only; a real model request fails the test.
models.ALLOW_MODEL_REQUESTS = False
