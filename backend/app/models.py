"""Data models for the parsed recipe plus the JSON schema Ollama is constrained to.

The schema is intentionally hand-written and "flat" (no $ref / $defs) because
Ollama turns it into a generation grammar, and flat schemas are the most broadly
compatible across model/runtime versions.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class Ingredient(BaseModel):
    name: str = ""          # שם הרכיב, e.g. "קמח לבן"
    amount: str = ""        # הכמות as written, e.g. "2", "1/2", "2-3"
    unit: str = ""          # יחידת מידה, e.g. "כוסות", "גרם", "כפית"
    section: str = ""       # sub-recipe/component, e.g. "לרוטב"; "" = main list


class Stage(BaseModel):
    step: int = 0           # 1-based ordinal of the preparation step
    instruction: str = ""   # הוראת ההכנה in Hebrew
    section: str = ""       # sub-recipe/component this step belongs to; "" = main


class Recipe(BaseModel):
    title: str = ""
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    servings: str = ""
    prep_time: str = ""
    cook_time: str = ""
    total_time: str = ""
    ingredients: List[Ingredient] = Field(default_factory=list)
    stages: List[Stage] = Field(default_factory=list)


class RecipeResult(Recipe):
    """What the API returns: the recipe plus provenance."""
    source_url: str = ""
    model: str = ""
    image_url: str = ""
    mealie: Optional[dict] = None


class ParseRequest(BaseModel):
    url: str
    send_to_mealie: bool = False
    model: Optional[str] = None


# --- JSON schema handed to Ollama's `format` field -------------------------

RECIPE_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "servings": {"type": "string"},
        "prep_time": {"type": "string"},
        "cook_time": {"type": "string"},
        "total_time": {"type": "string"},
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "amount": {"type": "string"},
                    "unit": {"type": "string"},
                    "section": {"type": "string"},
                },
                "required": ["name", "amount", "unit", "section"],
            },
        },
        "stages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "integer"},
                    "instruction": {"type": "string"},
                    "section": {"type": "string"},
                },
                "required": ["step", "instruction", "section"],
            },
        },
    },
    "required": [
        "title",
        "description",
        "tags",
        "servings",
        "prep_time",
        "cook_time",
        "total_time",
        "ingredients",
        "stages",
    ],
}
