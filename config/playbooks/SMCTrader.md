# Playbook SMCTrader (TJR-Stil)
1. Jeder Fill: Lag der Einstieg im FVG, der Stop hinter dem Sweep-Extrem, das Ziel bei mind. 2R? Abweichung nennen.
2. Jeder geschlossene Trade: Ergebnis in R mit FACTS.recent_trade_results und FACTS.journal vergleichen.
3. Verlustserie eines Marktes/Setups ueber der Backtest-Erwartung -> tighten_risk befristet, mit Beleg.
4. Abgelehnte Signale (TRADE_REJECTED/SIGNAL_BLOCKED): Grund wiedergeben, keine Umgehung vorschlagen.
5. Nie Orders platzieren, verschieben oder schliessen; nie Limits lockern.
