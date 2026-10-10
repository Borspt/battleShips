"""Модель партии морского боя с подсчётом ачивок.

Классика: 10x10, флот 4,3,3,2,2,2,1,1,1,1, без касаний даже углом, попадание
даёт ещё выстрел, потопление объявляется. Стрелок: карта вероятностей по
допустимым размещениям кораблей + добивание; в эндшпиле (<=2 корабля, нет
недобитых) — точный перебор согласованных расстановок.

Политики «нашей» стороны (флаги в Policy) включают тактики ачивок.
Соперник — тот же стрелок без бонусов; он перестреливает клетку DEFENDED.
"""
import sys, json, time
import numpy as np
from dataclasses import dataclass, field, asdict

N = 10
CELLS = N * N
FLEET = [4, 3, 3, 2, 2, 2, 1, 1, 1, 1]
RC = np.array([(i // N, i % N) for i in range(CELLS)])
MAN = (np.abs(RC[:, None, 0] - RC[None, :, 0]) + np.abs(RC[:, None, 1] - RC[None, :, 1])).astype(np.int16)


def _build():
    PL = {}
    for s in (1, 2, 3, 4):
        cells = []
        orients = [(0, 1)] if s == 1 else [(0, 1), (1, 0)]
        for dr, dc in orients:
            for r in range(N):
                for c in range(N):
                    rr, cc = r + dr * (s - 1), c + dc * (s - 1)
                    if rr < N and cc < N:
                        cells.append([(r + dr * k) * N + (c + dc * k) for k in range(s)])
        n = len(cells)
        P = np.zeros((n, CELLS), np.float32)
        H = np.zeros((n, CELLS), np.float32)
        for i, cl in enumerate(cells):
            for x in cl:
                P[i, x] = 1
                r, c = divmod(x, N)
                for a in (-1, 0, 1):
                    for b in (-1, 0, 1):
                        if 0 <= r + a < N and 0 <= c + b < N:
                            H[i, (r + a) * N + c + b] = 1
        D = np.min(MAN[np.array(cells)], axis=1).astype(np.int8)  # (n,100) dist from placement to each q
        PL[s] = (P, H, D, P.astype(bool), cells)
    return PL


PL = _build()
NB8 = np.zeros((CELLS, CELLS), bool)
for i in range(CELLS):
    r, c = divmod(i, N)
    for a in (-1, 0, 1):
        for b in (-1, 0, 1):
            if 0 <= r + a < N and 0 <= c + b < N:
                NB8[i, (r + a) * N + c + b] = True
INIT_DENS = sum(PL[s][0].sum(0) * FLEET.count(s) for s in (1, 2, 3, 4))


def random_fleet(rng):
    while True:
        forb = np.zeros(CELLS, np.float32)
        ships = []
        ok = True
        for s in FLEET:
            P, H, _, _, cl = PL[s]
            valid = np.nonzero(P @ forb == 0)[0]
            if len(valid) == 0:
                ok = False
                break
            i = rng.choice(valid)
            ships.append(list(cl[i]))
            forb = np.maximum(forb, H[i])
        if ok:
            return ships


def anti_density_fleet(rng, tries=300):
    best, bs = None, 1e18
    for _ in range(tries):
        f = random_fleet(rng)
        sc = sum(INIT_DENS[x] for sh in f for x in sh)
        if sc < bs:
            best, bs = f, sc
    return best


class Board:
    """Истинная доска цели."""

    def __init__(self, ships):
        self.ships = ships
        self.sid = -np.ones(CELLS, int)
        for k, sh in enumerate(ships):
            for x in sh:
                self.sid[x] = k
        self.hitc = [0] * len(ships)
        self.alive = len(ships)
        self.shield = False
        self.mines = set()

    def unsunk_cells(self):
        return [x for k, sh in enumerate(self.ships) if self.hitc[k] < len(sh) for x in sh]

    def fire(self, x):
        """-> 'miss'|'hit'|'sunk'|'defended'|'mine', would_sink"""
        k = self.sid[x]
        if k < 0:
            if x in self.mines:
                self.mines.discard(x)
                return "mine", False
            return "miss", False
        would_sink = self.hitc[k] + 1 == len(self.ships[k])
        if self.shield:
            self.shield = False
            return "defended", would_sink
        self.hitc[k] += 1
        if would_sink:
            self.alive -= 1
            return "sunk", True
        return "hit", False


class Know:
    """Знание стрелка о доске цели."""

    def __init__(self):
        self.shot = np.zeros(CELLS, bool)
        self.empty = np.zeros(CELLS, bool)
        self.hits = np.zeros(CELLS, bool)
        self.sunk = np.zeros(CELLS, bool)
        self.rem = {4: 1, 3: 2, 2: 3, 1: 4}
        self.defended = []
        self.cfg = None  # (cells bool m×100, D int8 m×100) в эндшпиле

    def nrem(self):
        return sum(self.rem.values())

    def blocked(self):
        return self.empty | self.sunk

    def density(self, sizes=None):
        bl = self.blocked().astype(np.float32)
        hv = self.hits.astype(np.float32)
        has_hits = self.hits.any()
        tgt = np.zeros(CELLS, np.float32)
        hunt = np.zeros(CELLS, np.float32)
        for s, c in self.rem.items():
            if c == 0 or (sizes is not None and s not in sizes):
                continue
            P, H = PL[s][0], PL[s][1]
            ok = (P @ bl) == 0
            if has_hits:
                ph = P @ hv
                hh = H @ hv
                sel = ok & (ph > 0) & (hh == ph)
                if sel.any():
                    w = np.where(sel, c * (20.0 ** (ph - 1)), 0).astype(np.float32)
                    tgt += w @ P
                sel2 = ok & (hh == 0)
            else:
                sel2 = ok
            hunt += sel2.astype(np.float32) * c @ P
        cand = ~(self.shot | self.blocked() | self.hits)
        if has_hits and (tgt * cand).sum() > 0:
            return tgt * cand, True
        return hunt * cand, False

    def choose(self, rng, sizes=None):
        if self.defended:
            return self.defended[0], 1.0
        if self.cfg is not None:
            cells = self.cfg[0]
            f = cells.mean(0) * ~(self.shot)
            m = f.max()
            idx = np.flatnonzero(f >= m - 1e-12)
            return int(rng.choice(idx)), float(m)
        d, tmode = self.density(sizes)
        tot = d.sum()
        if tot <= 0:
            idx = np.flatnonzero(~(self.shot | self.hits))
            return int(rng.choice(idx)), 0.0
        m = d.max()
        idx = np.flatnonzero(d >= m * (1 - 1e-6))
        x = int(rng.choice(idx))
        # грубая оценка P(попадания) в клетку
        if tmode:
            p = min(1.0, m / max(1e-9, self._tgt_mass()))
        else:
            p = min(1.0, m / max(1e-9, tot) * sum(s * c for s, c in self.rem.items()))
        return x, p

    def _tgt_mass(self):
        # сумма весов размещений, покрывающих недобитые попадания (знаменатель для P(hit))
        bl = self.blocked().astype(np.float32)
        hv = self.hits.astype(np.float32)
        tot = 0.0
        for s, c in self.rem.items():
            if c == 0:
                continue
            P, H = PL[s][0], PL[s][1]
            ok = (P @ bl) == 0
            ph = P @ hv
            hh = H @ hv
            sel = ok & (ph > 0) & (hh == ph)
            tot += float(np.where(sel, c * 20.0 ** (ph - 1), 0).sum())
        return tot

    def update(self, x, res):
        if res == "defended":
            if x not in self.defended:
                self.defended.append(x)
            if self.cfg is not None:
                keep = self.cfg[0][:, x]
                self.cfg = (self.cfg[0][keep], self.cfg[1][keep])
            return
        if x in self.defended:
            self.defended.remove(x)
        self.shot[x] = True
        if res in ("miss", "mine"):
            self.empty[x] = True
            if self.cfg is not None:
                keep = ~self.cfg[0][:, x]
                self.cfg = (self.cfg[0][keep], self.cfg[1][keep])
            return
        self.hits[x] = True
        if self.cfg is not None:
            keep = self.cfg[0][:, x]
            self.cfg = (self.cfg[0][keep], self.cfg[1][keep])
        if res == "sunk":
            cl = {x}
            st = [x]
            while st:
                y = st.pop()
                r, c = divmod(y, N)
                for a, b in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    if 0 <= r + a < N and 0 <= c + b < N:
                        z = (r + a) * N + c + b
                        if self.hits[z] and z not in cl:
                            cl.add(z)
                            st.append(z)
            cl = list(cl)
            self.hits[cl] = False
            self.sunk[cl] = True
            ring = NB8[cl].any(0) & ~self.sunk
            self.empty |= ring
            self.rem[len(cl)] -= 1
            self.cfg = None  # пересобрать перебор по оставшимся кораблям

    def maybe_enter_cfg(self):
        if self.cfg is not None or self.nrem() > 2 or self.hits.any() or self.defended or self.nrem() == 0:
            return
        sizes = [s for s, c in self.rem.items() for _ in range(c)]
        bl = self.blocked().astype(np.float32)
        if len(sizes) == 1:
            s = sizes[0]
            P, H, D, Pb, _ = PL[s]
            v = np.flatnonzero((P @ bl) == 0)
            self.cfg = (Pb[v], D[v])
            return
        s1, s2 = sizes
        P1, H1, D1, B1, _ = PL[s1]
        P2, H2, D2, B2, _ = PL[s2]
        v1 = np.flatnonzero((P1 @ bl) == 0)
        v2 = np.flatnonzero((P2 @ bl) == 0)
        comp = (H1[v1] @ P2[v2].T) == 0
        if s1 == s2:
            comp = np.triu(comp, 1)
        i, j = np.nonzero(comp)
        a, b = v1[i], v2[j]
        self.cfg = (B1[a] | B2[b], np.minimum(D1[a], D2[b]))


def best_query(K):
    cells, D = K.cfg
    m = len(D)
    if m <= 1:
        return None, 0.0
    bestH, bq = -1, None
    for q in range(CELLS):
        if K.shot[q]:
            continue
        cnt = np.bincount(D[:, q].astype(np.int64), minlength=1)
        p = cnt[cnt > 0] / m
        Hq = float(-(p * np.log2(p)).sum())
        if Hq > bestH:
            bestH, bq = Hq, q
    return bq, bestH


def apply_query(K, board, q):
    uc = board.unsunk_cells()
    d = int(MAN[q, uc].min()) if uc else 99
    if K.cfg is not None:
        keep = K.cfg[1][:, q] == d
        K.cfg = (K.cfg[0][keep], K.cfg[1][keep])
    # клетки ближе d — пустые
    near = (MAN[q] < d) & ~K.shot & ~K.hits
    K.empty |= near
    return d


@dataclass
class Policy:
    dist_endgame: int = 0        # DISTANCE в эндшпиле (<=2 корабля)
    streak_dist: int = 0         # DISTANCE для «Очереди»
    streak_from: int = 3
    streak_pmin: float = 0.9
    decap: bool = False          # «Обезглавливание»: сначала 4-палубник
    shield_sword: bool = False   # щит, когда у нашего раненого 1 целая клетка
    ss_max: int = 3
    shield_early: int = 0        # щиты в первых ходах соперника (Невидимка)
    mines: bool = False          # «Сапёр»: мины в предсказанный выстрел
    mine_pmin: float = 0.5
    salvo_2ships: bool = False   # «Залп-убийца»
    anti_density: bool = False   # расстановка против карты вероятностей
    opp_anti_density: bool = False
    solo: bool = False


def play(rng, pol: Policy):
    us_f = anti_density_fleet(rng) if pol.anti_density else random_fleet(rng)
    op_f = anti_density_fleet(rng) if pol.opp_anti_density else random_fleet(rng)
    us_b, op_b = Board(us_f), Board(op_f)  # us_b — наш флот, по нему стреляет соперник
    Kus, Kop = Know(), Know()              # Kus: наше знание о флоте соперника
    turn = int(rng.integers(2))            # 0 — мы
    st = dict(us_shots=0, us_hits=0, streak=0, maxstreak=0, first_sunk=None, op_shots=0,
              op_hits_first15=0, op_first15_n=0, shields=0, mines=0, dist=0, salvo=0,
              shield_sword=0, mine_trig=0, salvo_sinks=0, salvo_used=False, decap_on=pol.decap,
              entered2=False, op_shots_at_first_hit=None)
    skip_op = False
    dist_left = 3
    shields_left = 3
    mines_left = 3
    salvo_left = 1
    spent = 0
    while True:
        if turn == 0:
            # ---- бонусная фаза ----
            Kus.maybe_enter_cfg()
            if Kus.cfg is not None and Kus.nrem() == 2:
                st["entered2"] = True
            # щит: «Щит и меч»
            if pol.shield_sword and shields_left > 0 and not us_b.shield and st["shields"] < pol.ss_max:
                for k, sh in enumerate(us_b.ships):
                    if 0 < us_b.hitc[k] == len(sh) - 1:
                        us_b.shield = True
                        shields_left -= 1
                        st["shields"] += 1
                        spent += 2
                        break
            if pol.shield_early and shields_left > 0 and not us_b.shield and st["op_shots"] < 15 \
                    and st["shields"] < pol.shield_early:
                us_b.shield = True
                shields_left -= 1
                st["shields"] += 1
                spent += 2
            # мины: предсказать первый выстрел соперника в следующем ходу
            if pol.mines and mines_left > 0 and not skip_op:
                if Kop.defended:
                    pass
                else:
                    d, _ = Kop.density()
                    if d.sum() > 0:
                        m = d.max()
                        top = np.flatnonzero(d >= m * (1 - 1e-6))
                        empt = [x for x in top if us_b.sid[x] < 0 and x not in us_b.mines]
                        use = empt[:mines_left]
                        p = len(use) / len(top)
                        if use and p >= pol.mine_pmin:
                            for x in use:
                                us_b.mines.add(x)
                                mines_left -= 1
                                st["mines"] += 1
                                spent += 1
            # DISTANCE в эндшпиле
            if pol.dist_endgame and Kus.cfg is not None:
                while dist_left > 0 and st["dist"] < pol.dist_endgame:
                    q, Hq = best_query(Kus)
                    if q is None or Hq < 0.5:
                        break
                    apply_query(Kus, op_b, q)
                    dist_left -= 1
                    st["dist"] += 1
                    spent += 1
            # SALVO для «Залпа-убийцы»
            in_salvo = False
            if pol.salvo_2ships and salvo_left and Kus.cfg is not None and Kus.nrem() == 2 \
                    and (st["dist"] >= min(2, pol.dist_endgame) or dist_left == 0):
                in_salvo = True
                salvo_left = 0
                st["salvo_used"] = True
                spent += 4
            misses_budget = 3 if in_salvo else 1
            # ---- стрельба ----
            while True:
                Kus.maybe_enter_cfg()
                sizes = None
                if st["decap_on"]:
                    if Kus.rem[4] == 0:
                        st["decap_on"] = False
                    else:
                        sizes = (4,)
                x, p = Kus.choose(rng, sizes)
                # «Очередь»: проверить клетку запросом, если серия под угрозой
                while (pol.streak_dist and dist_left > 0 and st["dist"] < pol.streak_dist
                       and st["streak"] >= pol.streak_from and p < pol.streak_pmin
                       and Kus.cfg is None and not Kus.defended):
                    d = apply_query(Kus, op_b, x)
                    dist_left -= 1
                    st["dist"] += 1
                    spent += 1
                    if d == 0:
                        p = 1.0
                        break
                    x, p = Kus.choose(rng, sizes)
                res, _ = op_b.fire(x)
                Kus.update(x, res)
                st["us_shots"] += 1
                if res in ("hit", "sunk"):
                    st["us_hits"] += 1
                    st["streak"] += 1
                    st["maxstreak"] = max(st["maxstreak"], st["streak"])
                    if res == "sunk":
                        if st["first_sunk"] is None:
                            st["first_sunk"] = len(op_b.ships[op_b.sid[x]])
                        if in_salvo:
                            st["salvo_sinks"] += 1
                        if op_b.alive == 0:
                            return finish(st, True, us_b, spent)
                    continue
                st["streak"] = 0
                misses_budget -= 1
                if misses_budget <= 0:
                    break
            turn = 1
        else:
            if pol.solo:
                turn = 0
                continue
            if skip_op:
                skip_op = False
                turn = 0
                continue
            while True:
                Kop.maybe_enter_cfg()
                x, _ = Kop.choose(rng)
                res, would_sink = us_b.fire(x)
                Kop.update(x, res)
                if res != "defended" or True:
                    pass
                st["op_shots"] += 1
                if st["op_shots"] <= 15:
                    st["op_first15_n"] += 1
                    if res in ("hit", "sunk"):
                        st["op_hits_first15"] += 1
                if res in ("hit", "sunk"):
                    if st["op_shots_at_first_hit"] is None:
                        st["op_shots_at_first_hit"] = st["op_shots"]
                    if res == "sunk" and us_b.alive == 0:
                        return finish(st, False, us_b, spent)
                    continue
                if res == "defended" and would_sink:
                    st["shield_sword"] += 1
                if res == "mine":
                    st["mine_trig"] += 1
                    skip_op = True
                break
            turn = 0


def finish(st, win, us_b, spent):
    st["win"] = win
    st["alive"] = us_b.alive
    st["lost"] = 10 - us_b.alive
    st["spent"] = spent
    return st


def run(pol, n, seed):
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        out.append(play(rng, pol))
    return out


if __name__ == "__main__":
    name, n, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    pol = Policy(**json.loads(sys.argv[4]))
    t = time.time()
    res = run(pol, n, seed)
    with open(f"/home/claude/ach/out_{name}.json", "w") as f:
        json.dump(dict(policy=asdict(pol), n=n, seed=seed, games=res, sec=time.time() - t), f)
    print(name, "done", round(time.time() - t, 1), "s")
