"""Strategy implementations, one module per catalog category.

`build(sdef, params)` returns the runnable instance for a catalog entry.
Every class must implement the logic its catalog `logic` string documents —
the catalog is the contract, this package is the implementation.
"""
from __future__ import annotations

from app.lab.strategies import chain_oi, futures, opt_buy, opt_sell, quant, vol

_REGISTRY = {}
for mod in (futures, opt_buy, opt_sell, chain_oi, vol, quant):
    _REGISTRY.update(mod.STRATEGIES)


def build(sdef, params: dict):
    try:
        cls = _REGISTRY[sdef.sid]
    except KeyError:
        raise KeyError("no implementation for %s" % sdef.sid)
    return cls(sdef, params)


def implemented_sids() -> list:
    return sorted(_REGISTRY.keys())
