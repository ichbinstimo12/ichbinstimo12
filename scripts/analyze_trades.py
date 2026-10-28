"""Auswertung der echten Trades aus firm_events.db:  python scripts/analyze_trades.py [pfad/zur/firm_events.db]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.firm.money import journal  # noqa: E402


def main(db):
    ev = journal.load_events(db)
    trades = journal.reconstruct_trades(ev)
    eq = journal.equity_summary(ev)
    st = journal.stats(trades)
    print("=== KONTO ===")
    for k, v in eq.items():
        print(f"  {k}: {v:.2f}")
    print("\n=== TRADES (aus Broker-Kontostaenden rekonstruiert) ===")
    for t in trades:
        pnl = "offen" if t["pnl"] is None else f"{t['pnl']:+.2f}"
        r = "" if t["r"] is None else f" ({t['r']:+.2f} R)"
        mins = "" if t.get("minutes") is None else f", {t['minutes']:.0f} min"
        print(f"  {t['opened'][:16]} {t['epic']} {t['side']} size {t['size']}: {pnl}{r}{mins}")
    print("\n=== KENNZAHLEN ===")
    for k, v in st.items():
        print(f"  {k}: {v}")
    n = st.get("closed", 0)
    print(f"\nAussagekraft: {n} geschlossene Trades. Fuer ein Urteil ueber eine Strategie braucht man mindestens ~100.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "firm_events.db")
