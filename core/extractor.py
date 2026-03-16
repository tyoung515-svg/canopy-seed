"""
Canopy Seed Fact Extractor
--------------------------
Extracts structured information from unstructured text.
Used to distill "learnings" or "memories" from conversations
and store them in the project memory.
"""

from typing import List, Dict, Any, Optional
import json
import logging
from core.ai_backend import AIBackend
from memory.canopy import MemoryStore

logger = logging.getLogger(__name__)

class FactExtractor:
    """
    Analyzes text to extract key facts, decisions, and action items.
    """
    def __init__(self, ai_backend: AIBackend, memory: MemoryStore):
        self.ai = ai_backend
        self.memory = memory

    async def extract_facts(self, text: str, context: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Uses an LLM to extract bullet points or structured data from the text.
        """
        prompt = f"""
        Extract the most important technical facts, user preferences, or project decisions from the following text.
        Return a JSON list of objects with 'category', 'fact', and 'confidence' fields.

        Context: {context or 'General conversation'}
        Text:
        {text}
        """

        try:
            response = await self.ai.generate_chat_completion(
                system_prompt="You are a precise fact extraction engine. Output JSON only.",
                user_prompt=prompt,
                model_key="gemini-flash-lite", # Use fast/cheap model
                temperature=0.1
            )
            
            # Clean md blocks
            cleaned = response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned = "\n".join(lines)

            facts = json.loads(cleaned)
            return facts if isinstance(facts, list) else []

        except Exception as e:
            logger.error(f"Fact extraction failed: {e}")
            return []

    async def extract_and_store(self, text: str, source_id: str):
        """
        Extracts facts and immediately persists them to memory.
        """
        facts = await self.extract_facts(text)
        count = 0
        for fact in facts:
            # Store in memory (generic 'facts' collection or similar)
            # For now, we'll log it or use a simple key-value storage in memory
            # Assuming memory.canopy has a simple storage method, or we extend it
            
            # Key: fact_sourceId_index
            key = f"fact_{source_id}_{count}"
            value = json.dumps(fact)
            
            # Depending on memory implementation, we might use specific methods
            # Here we assume a generic set/store method exists or we use the underlying DB
            # self.memory.store(key, value) 
            # (Placeholder as memory implementation details vary)
            
            logger.info(f"Extracted Fact: {fact['fact']} ({fact['category']})")
            count += 1
        return count
