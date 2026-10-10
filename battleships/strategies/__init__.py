from functools import partial
from typing import Callable

from ..placement import edge_placement
from .base import Strategy
from .baseline import BaselineStrategy
from .probability import HARD_BIAS, ProbabilityStrategy

# Имена для симулятора. "edges" заодно служит моделью встроенного бота HARD:
# он ставит корабли по краям и стреляет от центра.
STRATEGIES: dict[str, Callable[..., Strategy]] = {
    "baseline": BaselineStrategy,
    "probability": ProbabilityStrategy,
    "edges": partial(ProbabilityStrategy, placement=edge_placement),
    "edges-biased": partial(ProbabilityStrategy, placement=edge_placement, edge_bias=HARD_BIAS),
}
