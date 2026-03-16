"""Skill registry with auto-discovery from the skills package."""

import importlib
import inspect
import logging
import pkgutil
from pathlib import Path

from skills.base import Skill

logger = logging.getLogger(__name__)


class SkillRegistry:
    def __init__(self):
        self._skills_by_trigger: dict[str, Skill] = {}
        self._skills: list[Skill] = []
        self.discover_and_register()

    def discover_and_register(self):
        skills_dir = Path(__file__).parent
        for module_info in pkgutil.iter_modules([str(skills_dir)]):
            module_name = module_info.name
            if module_name in {"__init__", "base", "registry"}:
                continue

            full_module_name = f"skills.{module_name}"
            try:
                module = importlib.import_module(full_module_name)
            except Exception as e:
                logger.error(f"Failed to import skill module '{full_module_name}': {e}")
                continue

            for _, cls in inspect.getmembers(module, inspect.isclass):
                if cls is Skill:
                    continue
                if not issubclass(cls, Skill):
                    continue

                try:
                    skill = cls()
                    self.register(skill)
                except Exception as e:
                    logger.error(f"Failed to initialize skill '{cls.__name__}': {e}")

    def register(self, skill: Skill):
        self._skills.append(skill)
        if not getattr(skill, "enabled", True):
            return
        for trigger in skill.triggers:
            normalized = trigger.strip().lower()
            if not normalized:
                continue
            self._skills_by_trigger[normalized] = skill

    def get(self, command_name: str) -> Skill | None:
        return self._skills_by_trigger.get(command_name.strip().lower())

    def list_skills(self) -> list[Skill]:
        return list(self._skills)
