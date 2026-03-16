"""Base abstractions for pluggable skills."""

from abc import ABC, abstractmethod


class Skill(ABC):
    name: str = "unnamed"
    description: str = ""
    triggers: list[str] = []
    enabled: bool = True

    @abstractmethod
    async def execute(self, args: str, update, context) -> str:
        """Execute the skill and return a response string."""
