"""Closing Day Strategy — the overnight continuation study.

One hypothesis, tested end to end: at 15:00 IST compare NIFTY to the previous
day's close; buy the ATM PE when it is lower and the ATM CE when it is higher;
sell at 09:50 the next morning. The claim under test is that the day's late
direction carries overnight.

Two layers, deliberately kept apart because their evidence is not the same
quality:

  LAYER 1 (measured)  NIFTY points from 15:00 to next-day 09:50, split by the
                      signal. Real index prints, no model, no assumptions.
                      If there is no edge here there is no strategy.

  LAYER 2 (modelled)  The ATM option P&L those points would have produced.
                      Kite deletes expired contracts (an expired token returns
                      "invalid token"), so a year of real premiums cannot be
                      fetched at any price. Layer 2 therefore prices the option
                      with Black-Scholes on REAL spot and REAL India VIX, with
                      an ATM-IV term-structure calibrated from the only real
                      option quotes this machine holds (app/condor's chain
                      snapshots). app/closing/validate.py measures that model
                      against those quotes and publishes the error — no number
                      in Layer 2 is worth more than that error bar.

Self-contained like the Patterns Module: owns its own VIX store and results
JSON, places no orders, touches no signal-engine state. It reads the patterns
candle store read-only for the NIFTY 5-min spine rather than keeping a second
copy of the same 55k bars.
"""
