"""
LLM module initialization
"""

from openevolve.llm.base import LLMInterface
from openevolve.llm.ensemble import LLMEnsemble
from openevolve.llm.openai import OpenAILLM

# ClaudeCodeLLM is optional (requires `claude-agent-sdk`); import lazily.
__all__ = ["LLMInterface", "OpenAILLM", "LLMEnsemble", "ClaudeCodeLLM"]


def __getattr__(name):
    if name == "ClaudeCodeLLM":
        from openevolve.llm.claude_code import ClaudeCodeLLM
        return ClaudeCodeLLM
    raise AttributeError(name)
