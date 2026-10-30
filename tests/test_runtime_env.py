"""Dauerlauf-Hilfen: Wachhalten, Windows-Rauschen, Fehlerdrossel, Ollama-Autostart, Reconciler-Alarm."""
import asyncio
from types import SimpleNamespace

from src.firm.bus.event_store import EventStore
from src.firm.money.reconciler import Reconciler
from src.firm.ops import runtime_env as rt


def test_keep_awake_only_on_windows_and_can_be_disabled(monkeypatch):
    calls = []
    k32 = SimpleNamespace(SetThreadExecutionState=lambda f: calls.append(f) or 1)
    assert rt.keep_awake("linux", k32) is False and not calls
    assert rt.keep_awake("win32", k32) is True and calls == [rt.ES_CONTINUOUS | rt.ES_SYSTEM_REQUIRED]
    monkeypatch.setenv("FIRM_KEEP_AWAKE", "0")
    assert rt.keep_awake("win32", k32) is False


def test_remote_disconnects_are_noise_but_real_errors_are_not():
    assert rt.is_benign_disconnect({"exception": ConnectionResetError(10054, "reset")})
    assert not rt.is_benign_disconnect({"exception": ValueError("bug")})
    assert not rt.is_benign_disconnect({"message": "no exception"})


def test_noise_filter_passes_real_errors_to_default_handler():
    loop = asyncio.new_event_loop()
    seen = []
    loop.set_exception_handler(lambda lp, ctx: seen.append(ctx))
    rt.install_loop_noise_filter(loop)
    loop.call_exception_handler({"message": "x", "exception": ConnectionResetError()})
    loop.call_exception_handler({"message": "y", "exception": RuntimeError("real")})
    loop.close()
    assert [c["message"] for c in seen] == ["y"]


def test_error_throttle_logs_once_per_window_then_reports_suppressed():
    t = [0.0]
    th = rt.ErrorThrottle(60, now_fn=lambda: t[0])
    assert th.should_log("dns") == 0
    assert th.should_log("dns") is None and th.should_log("dns") is None
    assert th.should_log("other") == 0
    t[0] = 61
    assert th.should_log("dns") == 2


def test_ollama_autostart_only_local_installed_and_rate_limited(monkeypatch):
    monkeypatch.setattr(rt, "_ollama_started_at", None)
    started = []
    popen = lambda args, **kw: started.append(args)
    t = [1000.0]
    assert not rt.try_start_ollama("http://10.0.0.5:11434", which=lambda _: "/bin/ollama", popen=popen)
    assert not rt.try_start_ollama("http://localhost:11434", which=lambda _: None, popen=popen)
    assert rt.try_start_ollama("http://localhost:11434", which=lambda _: "/bin/ollama", popen=popen, now_fn=lambda: t[0])
    assert started == [["/bin/ollama", "serve"]]
    assert not rt.try_start_ollama("http://localhost:11434", which=lambda _: "/bin/ollama", popen=popen,
                                   now_fn=lambda: t[0] + 60)
    monkeypatch.setenv("FIRM_OLLAMA_AUTOSTART", "0")
    monkeypatch.setattr(rt, "_ollama_started_at", None)
    assert not rt.try_start_ollama("http://localhost:11434", which=lambda _: "/bin/ollama", popen=popen)


def test_reconciler_alarms_once_after_consecutive_errors_and_reports_recovery(tmp_path):
    store = EventStore(str(tmp_path / "e.db"))
    rec = Reconciler(broker=None, event_store=store)
    for _ in range(Reconciler.ALARM_AFTER + 3):
        rec._record_error(OSError("getaddrinfo failed"))
    assert len(store.get_events(types=["RECONCILER_BLIND"])) == 1
    rec._record_success()
    assert store.get_events(types=["RECONCILER_RECOVERED"]) and rec.consecutive_errors == 0


def test_agents_work_in_parallel_but_bounded(monkeypatch):
    from src.firm import main as firm_main
    monkeypatch.setenv("FIRM_AGENT_PARALLEL", "2")
    monkeypatch.setenv("FIRM_TICK_SECONDS", "0")
    orch = firm_main.FirmOrchestrator.__new__(firm_main.FirmOrchestrator)
    names = ["A", "B", "C", "D"]
    pending = {n: [{"task_id": f"t{n}"}] for n in names}
    running, peak, done = [0], [0], []

    async def step(agent, obs, task_id, facts=None, on_action=None):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0.01)
        running[0] -= 1
        return {"ok": True}

    orch.agent_runtime = SimpleNamespace(active_agents={n: {} for n in names}, step=step)
    orch.task_queue = SimpleNamespace(next_pending=lambda n: pending[n].pop() if pending[n] else None,
                                      complete=lambda tid, r: done.append(tid), fail=lambda tid, e: None)
    orch._build_observation = lambda n, t: ""
    orch._facts = lambda n: {}
    orch._archive_task = lambda *a, **k: None
    orch.handle_agent_action = None
    orch.is_running = True

    async def run():
        loop_task = asyncio.create_task(orch.agent_loop())
        while len(done) < 4:
            await asyncio.sleep(0.005)
        orch.is_running = False
        await loop_task

    asyncio.run(run())
    assert sorted(done) == ["tA", "tB", "tC", "tD"] and peak[0] == 2
