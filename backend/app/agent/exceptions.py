class AgentError(Exception):
    """Base error for agent execution."""


class ModelNotConfiguredError(AgentError):
    """The model provider is not configured."""


class ModelCallError(AgentError):
    """The model call failed."""


class AgentTimeoutError(AgentError):
    """The run exceeded its timeout."""


class MaxIterationsExceededError(AgentError):
    """The run exceeded its iteration limit."""
