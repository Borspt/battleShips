"""Research two rejected bonus changes; no live bot changes or win-rate claim.

The first DISTANCE=0 already proves one singleton. Both policies then get one
remaining query. Enumerate EVERY legal second singleton and identical seeds.
The certain-shot guard did not save queries and slightly increased shots.
Two saved real positions also compare two vs three queries. These are fixed
endgames, not independent complete matches: seeds are not new observations.
"""
import json
from pathlib import Path
import random
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from battleships.rules import Rules
from battleships.simulator import Board
from battleships.strategies.haki import HakiEndgameStrategy
from battleships.strategies.probability import MIN_QUERY_BITS


def certain_unshot_cell(configs, blocked=0):
    """REJECTED experimental query guard, retained only for reproducibility."""
    if not configs:
        return None
    common = configs[0][0] & ~blocked
    for mask, _, _ in configs[1:]:
        common &= mask
        if not common:
            return None
    return (common & -common).bit_length() - 1 if common else None


class Legacy(HakiEndgameStrategy):
    def next_action(self):
        if self.distance_uses > 0 and not self.hits and not self.known_ship:
            configs = self._endgame_configs()
            if configs and len(configs) > 1:
                cell, information = self._best_query(configs)
                if information >= MIN_QUERY_BITS:
                    return 'DISTANCE', divmod(cell, self.rules.columns)
        return 'SHOOT', self.next_shot()


class Guarded(Legacy):
    def next_action(self):
        if not self.hits and not self.known_ship:
            cell = certain_unshot_cell(self._endgame_configs(), self.empty | self.hits | self.sunk)
            if cell is not None:
                return 'SHOOT', divmod(cell, self.rules.columns)
        return super().next_action()


def run(cls, target, seed, modifier):
    rules = Rules(rows=5, columns=5, fleet=(1, 1), modifier=modifier)
    shooter = cls(rules, random.Random(seed), distance_uses=2)
    board = Board([[(0, 0)], [target]])
    shooter.on_distance((0, 0), 0)
    shots = queries = 0
    while not board.destroyed and shots + queries < 25:
        kind, cell = shooter.next_action()
        if kind == 'DISTANCE':
            queries += 1
            shooter.on_distance(cell, board.distance(cell))
        else:
            shots += 1
            result = board.receive(cell)
            shooter.on_shot_result(cell, 'HIT' if result == 'SUNK' and modifier else result)
    assert board.destroyed
    return shots, queries


def main():
    result = {'status': 'Research only: guard rejected; third query not enabled'}
    targets = [(r, c) for r in range(5) for c in range(5) if max(r, c) > 1]
    for modifier in (None, 'SCHRODINGER_SHIP'):
        result[str(modifier)] = {}
        for cls in (Legacy, Guarded):
            data = [run(cls, target, seed, modifier) for target in targets for seed in range(10)]
            result[str(modifier)][cls.__name__] = dict(
                positions=len(data), meanShots=statistics.mean(s for s, q in data),
                meanAdditionalQueries=statistics.mean(q for s, q in data))
    for name in ('haki_endgame_position.json', 'haki_three_singletons.json'):
        fixture = json.loads((Path(__file__).resolve().parents[1] / 'tests' / name).read_text())
        result[name] = {}
        for uses in (0, 2, 3):
            data = []
            for seed in range(100):
                rules = Rules.from_message(fixture['rules'])
                shooter = Legacy(rules, random.Random(27000 + seed), distance_uses=uses)
                for shot in fixture['shots']:
                    c = shot['cell']
                    shooter.on_shot_result((c['row'], c['column']), shot['result'])
                board = Board([[tuple(c)] for c in fixture['actualRemaining']])
                shots = queries = 0
                while not board.destroyed and shots + queries < fixture['shotsLeft']:
                    kind, cell = shooter.next_action()
                    if kind == 'DISTANCE':
                        queries += 1
                        shooter.on_distance(cell, board.distance(cell))
                    else:
                        shots += 1
                        shooter.on_shot_result(cell, board.receive(cell))
                assert board.destroyed
                data.append((shots, queries))
            result[name][str(uses)] = dict(seeds=len(data),
                meanShots=statistics.mean(s for s, q in data),
                meanQueries=statistics.mean(q for s, q in data))
    result['economics'] = {
        'oneExtraQueryMinimumWinGainAtK20': 0.05,
        'oneExtraQueryMinimumWinGainAtK10': 0.1,
        'limitations': 'Unclipped Elo, own direct cost only. No win gain inferred from solo shots.'}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
