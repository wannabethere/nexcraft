from __future__ import annotations

from nexcraft_jobs.recipe import Recipe


class RecipeRegistry:
    def __init__(self) -> None:
        self._recipes: dict[tuple[str, str], Recipe] = {}

    def register(self, recipe: Recipe) -> None:
        key = (recipe.name, recipe.version)
        if key in self._recipes:
            raise ValueError(f"Duplicate recipe registration: {key}")
        self._recipes[key] = recipe

    def get(self, name: str, version: str) -> Recipe:
        try:
            return self._recipes[(name, version)]
        except KeyError as e:
            raise KeyError(f"No recipe registered for {name!r} version {version!r}") from e


GLOBAL_REGISTRY = RecipeRegistry()
