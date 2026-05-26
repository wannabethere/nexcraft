"""Recipes for nexcraft-jobs. A Recipe is the production unit of work — its
contract is: validate → extract → compute → persist. Compose dstools calls
inside `compute()` for portable analytics across any FedSQL source."""

from nexcraft_jobs.recipes.cross_source_flux import CrossSourceFluxRecipe

__all__ = ["CrossSourceFluxRecipe"]
