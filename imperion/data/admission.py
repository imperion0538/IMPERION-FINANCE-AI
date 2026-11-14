"""Temporary Radar symbol permissions. No orders and no changes to risk limits.

Optional narrowing allowlist + current eligible Radar row + A+/A deep analysis +
healthy data + current exchange filters are all required. Leases are never
restored from disk: restart requires fresh analysis and admission again.
"""
from __future__ import annotations

import math
import threading

FILTER_MAX_AGE_MS = 30 * 60_000


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def fresh(timestamp, now, age):
    return finite(timestamp) and finite(now) and timestamp > 0 and 0 <= now - timestamp < age


class RadarAdmission:
    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.RLock()
        self._leases = {}
        self._rejections = {}

    def revoke_missing(self, symbols):
        current = set(symbols)
        with self._lock:
            self._leases = {s: row for s, row in self._leases.items() if s in current}

    def clear(self):
        with self._lock:
            self._leases = {}

    def replace(self, ranked, deep, radar_updated, filters, filters_updated, now, healthy):
        leases, rejected = {}, {}
        ttl = self.cfg.radar_admission_ttl_seconds * 1000
        for row in ranked:
            symbol = row.get('symbol')
            reason = ''
            analysis = deep.get(symbol, {})
            f = filters.get(symbol)
            if not self.cfg.radar_trading_enabled:
                reason = 'RADAR_TRADING_DISABLED'
            elif self.cfg.radar_trade_allowlist and symbol not in self.cfg.radar_trade_allowlist:
                reason = 'OPTIONAL_RADAR_ALLOWLIST_RESTRICTION'
            elif symbol in self.cfg.symbols:
                continue
            elif row.get('eligible') is not True or not finite(row.get('score')) or not 0 <= row['score'] <= 1:
                reason = 'RADAR_INELIGIBLE'
            elif not finite(row.get('quote_volume')) or row['quote_volume'] < self.cfg.radar_min_quote_volume:
                reason = 'RADAR_LOW_LIQUIDITY'
            elif not fresh(radar_updated, now, ttl) or not fresh(analysis.get('ts'), now, ttl):
                reason = 'RADAR_EVIDENCE_STALE'
            elif analysis.get('approved') is not True or analysis.get('grade') not in ('A+', 'A') or analysis.get('direction') not in ('LONG', 'SHORT') or not analysis.get('setup'):
                reason = 'DEEP_ANALYSIS_NOT_APPROVED'
            elif not fresh(filters_updated, now, FILTER_MAX_AGE_MS):
                reason = 'EXCHANGE_FILTERS_STALE'
            elif f is None or f.status != 'TRADING' or f.contract_type != 'PERPETUAL':
                reason = 'EXCHANGE_SYMBOL_NOT_TRADABLE'
            elif not healthy(symbol):
                reason = 'RADAR_DATA_UNHEALTHY'
            elif len(leases) >= self.cfg.radar_max_active:
                reason = 'RADAR_ACTIVE_LIMIT'
            if reason:
                rejected[symbol] = reason
            else:
                leases[symbol] = {
                    'admitted_ms': now,
                    'expires_ms': min(now + ttl, radar_updated + ttl,
                                      analysis['ts'] + ttl, filters_updated + FILTER_MAX_AGE_MS),
                    'style': 'day', 'source': 'automatic_a_plus_or_a_radar',
                }
        with self._lock:
            self._leases, self._rejections = leases, rejected

    def symbols(self, now):
        with self._lock:
            return [s for s, row in self._leases.items()
                    if row['admitted_ms'] <= now < row['expires_ms']]

    def snapshot(self, now):
        with self._lock:
            return {'enabled': self.cfg.radar_trading_enabled,
                    'active': {s: dict(self._leases[s]) for s in self.symbols(now)},
                    'rejected': dict(self._rejections), 'style': 'day'}
