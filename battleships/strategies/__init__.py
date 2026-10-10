from .base import Strategy
from .baseline import BaselineStrategy
from .probability import ProbabilityStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    "baseline": BaselineStrategy,
    "probability": ProbabilityStrategy,
}
