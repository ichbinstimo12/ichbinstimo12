from pathlib import Path

DASHBOARD = Path("web/office.html")


def test_dashboard_exists_and_is_self_contained():
    html = DASHBOARD.read_text(encoding="utf-8")

    # Offline-first: no external scripts/stylesheets that can blank the page.
    assert 'src="http' not in html
    assert 'href="http' not in html
    assert "cdn." not in html

    # It must talk to the real firm API.
    assert "/api/status" in html
    assert "/api/agents" in html
    assert "/api/tasks" in html
    assert "/api/lessons" in html
    assert "/api/archive" in html
    assert "/ws/office" in html


def test_dashboard_has_a_trade_history_folder():
    html = DASHBOARD.read_text(encoding="utf-8")
    assert 'id="archive-panel"' in html
    assert "TRADE HISTORY" in html
    assert 'id="ledger"' in html
    assert 'id="archive-stats"' in html
    # the folder view must render real archived rows, not static examples
    assert "renderArchive" in html
    assert "archiv/trades" in html


def test_dashboard_shows_live_state_without_fake_numbers():
    html = DASHBOARD.read_text(encoding="utf-8")
    # Placeholders instead of hardcoded balances.
    assert "200.00 CHF" not in html
    assert 'id="balance">—' in html
    assert 'id="equity">—' in html
