"""Checks for quant.py against known textbook values. Run: python3 test_quant.py"""
import math

import quant as q


def close(a, b, tol=1e-3):
    assert a is not None and abs(a - b) <= tol, (a, b)


def main():
    # 1.2 log returns add up: 100 -> 110 -> 99 is -1% overall
    r = q.log_returns([100, 110, 99])
    close(sum(r), math.log(0.99), 1e-12)
    # 2.2 1% daily vol is about 15.87% a year
    close(q.annualize_vol(0.01, 252), 0.15875, 1e-4)
    # 2.1 sample sd
    close(q.stdev([2, 4, 4, 4, 5, 5, 7, 9]), 2.13809, 1e-4)
    # 4.2 / 5.2 a series against itself doubled: corr 1, beta 2
    m = [math.sin(i) / 100 for i in range(50)]
    c, b = q.corr_beta([2 * x for x in m], m)
    close(c, 1.0, 1e-9)
    close(b, 2.0, 1e-9)
    # 10.3 drawdown
    close(q.max_drawdown([1, 1, -1, -1, -1, 2]), -3.0, 1e-12)
    # 11.1 Kelly: 60% win, even money -> 20%
    close(q.kelly_binary(0.6, 1.0), 0.2, 1e-12)
    # 17.3 break-even at 2R with no cost is 1/3, and gambler's ruin gives the same 1/3
    close(q.breakeven_hit_rate(2), 1 / 3, 1e-12)
    close(q.random_walk_hit_first(100, 99, 102), 1 / 3, 1e-12)
    # 15.2 Black-Scholes: S=K=100, T=1, r=5%, vol=20% -> call 10.4506, put 5.5735
    close(q.bs_price(100, 100, 1, 0.05, 0.2, "C"), 10.4506, 1e-3)
    close(q.bs_price(100, 100, 1, 0.05, 0.2, "P"), 5.5735, 1e-3)
    # 15.3 put-call parity: C - P = S - K e^{-rT}
    close(q.bs_price(100, 110, 0.5, 0.04, 0.3, "C") - q.bs_price(100, 110, 0.5, 0.04, 0.3, "P"),
          100 - 110 * math.exp(-0.04 * 0.5), 1e-9)
    # 15.4 greeks of that call: delta 0.6368
    close(q.bs_greeks(100, 100, 1, 0.05, 0.2, "C")["delta"], 0.6368, 1e-3)
    # 15.5 implied vol recovers the input, including a 0DTE-style tiny T
    for T, K, kind in ((1, 100, "C"), (0.25, 90, "P"), (2 / (365 * 24), 101, "C")):
        px = q.bs_price(100, K, T, 0.04, 0.35, kind)
        close(q.implied_vol(px, 100, K, T, 0.04, kind), 0.35, 1e-3)
    assert q.implied_vol(0.0, 100, 100, 1, 0.04) is None
    # 13.5 VWAP
    bars = [{"h": 11, "l": 9, "c": 10, "v": 100}, {"h": 21, "l": 19, "c": 20, "v": 300}]
    close(q.vwap(bars), 17.5, 1e-12)
    # 16.4 PSR of a clearly positive series is high, of a symmetric one about 0.5
    close(q.psr([1, -1] * 20), 0.5, 1e-9)
    assert q.psr([2, -1, 2, 2, -1, 2, -1, 2, 2, -1]) > 0.8
    s = q.trade_stats([2, -1, -1, 2, -1])
    close(s["hit"], 0.4, 1e-12)
    close(s["avg_r"], 0.2, 1e-12)
    close(s["profit_factor"], 4 / 3, 1e-12)
    print("all quant checks passed")


if __name__ == "__main__":
    main()
