"""Central default values for Vigil.

Import from here rather than scattering literals across the codebase.
All values are overridable via environment variables so operator deployments
can change them without code changes.
"""

import os

# Fallback model ID used when no provider-specific model can be resolved
# (e.g. fresh install, DB unavailable, no ai_model_configs row).
# Operators on Ollama-only deployments should set this to their local model
# (e.g. "llama3.2:1b") so the failsafe never tries to call an Anthropic model.
DEFAULT_MODEL: str = os.getenv("DEFAULT_MODEL", "claude-sonnet-4-6")
