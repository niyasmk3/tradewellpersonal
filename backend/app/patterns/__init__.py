"""Patterns Module — standalone day-of-week / level / candlestick-frequency research.

Self-contained: owns its own SQLite candle store and results JSON, touches no
signal-engine state. NIFTY 50 index OHLC (token 256265) + NIFTYBEES volume
proxy (NSE indices report no volume on Kite), 5-minute bars, ~3 years.
"""
