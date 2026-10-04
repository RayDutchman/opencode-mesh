"""The shared Agent supervisor starts one child process per configured instance and owns their lifetime.

The children here are real short-lived interpreters, so process creation, signals, exit codes and
reaping are the real ones. Only the command each child runs is redirected, at the test boundary.
"""

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Modes: hold stays until killed, exit dies immediately to exercise restart backoff,
# ignore survives SIGTERM so the supervisor has to escalate,
# stable lives longer than the reset threshold and then fails.
CHILD = r"""
import os, signal, sys, time
mode, marker, lifetime = sys.argv[1], sys.argv[2], float(sys.argv[3])
with open(marker, "a") as handle:
    handle.write("%d\n" % os.getpid())
    handle.flush()
if mode == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == "exit":
    sys.exit(3)
if mode == "stable":
    time.sleep(lifetime)
    sys.exit(4)
while True:
    time.sleep(3600)
"""

# Stop timings exist in the shipped code and only need shortening where a test cares about
# ordering instead of waiting.
QUICK_STOP = {"SUPERVISOR_STOP_GRACE_SECONDS": 0.3, "SUPERVISOR_STOP_TIMEOUT_SECONDS": 10.0}
# The restart ladder is the new tuning surface; only the tests about restarting shorten it.
QUICK_BACKOFF = {"SUPERVISOR_BACKOFF_BASE_SECONDS": 0.1, "SUPERVISOR_BACKOFF_CAP_SECONDS": 0.8}
FAST = {**QUICK_STOP, **QUICK_BACKOFF}


def write_shared_config(tmp_path, instances):
    path = tmp_path / "config" / "agents.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "gateway_url": "https://127.0.0.1:1",
        "enroll_token": "token",
        "agents": {name: {"opencode_url": "http://127.0.0.1:4096"} for name in instances},
    }))
    return path


def child_pids(marker):
    if not Path(marker).exists():
        return []
    return [int(line) for line in Path(marker).read_text().split()]


def wait_until(predicate, timeout=15.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


async def await_until(predicate, timeout=15.0, interval=0.02):
    """Same deadline contract as wait_until, without blocking the loop the supervisor runs on."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def retry_delays(output, name):
    """The restart delays the supervisor actually reported for one instance."""
    pattern = rf"instance {re.escape(name)} exited with code \d+; retry in ([\d.]+)s"
    return [float(value) for value in re.findall(pattern, output)]


def redirect_children(monkeypatch, tmp_path, modes, **timings):
    """Point the supervisor at real interpreters whose behaviour each instance dictates.

    A mode is either a plain behaviour or a (behaviour, lifetime) pair for the ones that live
    for a while before failing. Only the child command is redirected; the backoff ladder is
    never stubbed, so the tests read the delays the supervisor really chose.
    """
    markers = {name: tmp_path / f"{name}.marker" for name in modes}

    def fake_command(config_path, name):
        mode, lifetime = modes[name], 3600.0
        if isinstance(mode, tuple):
            mode, lifetime = mode
        return [sys.executable, "-u", "-c", CHILD, mode, str(markers[name]), str(lifetime)]

    monkeypatch.setattr("src.main.agent_command", fake_command)
    for constant, value in timings.items():
        monkeypatch.setattr(f"src.main.{constant}", value)
    return markers


async def supervise(config_path, instances, stop):
    from src.main import run_agent_supervisor
    return await run_agent_supervisor(config_path, instances, stop)


def test_supervisor_refuses_a_configuration_without_the_shared_agents_map(tmp_path):
    """A single-instance configuration names no instances, so there is nothing to supervise."""
    from src.main import supervise_agent_instances
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"gateway_url": "https://127.0.0.1:1", "enroll_token": "t"}))
    with pytest.raises(ValueError):
        supervise_agent_instances(path)


def test_supervisor_refuses_an_empty_instance_set(tmp_path):
    """An empty mapping would leave a unit running that serves no device."""
    from src.main import supervise_agent_instances
    path = write_shared_config(tmp_path, {})
    with pytest.raises(ValueError):
        supervise_agent_instances(path)


@pytest.mark.parametrize("entry,name", [
    ("not-an-object", "beta"),
    ({"opencode_url": "http://127.0.0.1:4096"}, "../escape"),
    ({"opencode_url": "http://127.0.0.1:4096", "state_file": "chosen.json"}, "beta"),
])
def test_supervisor_refuses_instance_entries_it_cannot_serve(tmp_path, entry, name):
    """Malformed entries and hand-picked identity paths are rejected before anything is spawned."""
    from src.main import supervise_agent_instances
    path = tmp_path / "config" / "agents.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "gateway_url": "https://127.0.0.1:1", "enroll_token": "t", "agents": {name: entry}}))
    with pytest.raises(ValueError):
        supervise_agent_instances(path)


def test_supervisor_refuses_a_shared_identity_path(tmp_path):
    """The identity path is derived per instance; a shared one would collide across children."""
    from src.main import supervise_agent_instances
    path = tmp_path / "config" / "agents.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "gateway_url": "https://127.0.0.1:1", "enroll_token": "t",
        "state_file": "shared.json",
        "agents": {"beta": {"opencode_url": "http://127.0.0.1:4096"}}}))
    with pytest.raises(ValueError):
        supervise_agent_instances(path)


def test_child_command_targets_the_internal_worker_cli(tmp_path):
    """Each child is the ordinary single-instance Agent, so it keeps its own identity and retries."""
    from src.main import agent_command
    config = tmp_path / "config" / "agents.json"
    assert agent_command(config, "beta") == [
        sys.executable, "-m", "src.main", "--mode", "agent",
        "--config", str(config.resolve()), "--agent-instance", "beta"]


def test_supervisor_starts_every_instance(tmp_path, monkeypatch):
    markers = redirect_children(monkeypatch, tmp_path,
                                {"alpha": "hold", "beta": "hold", "gamma": "hold"}, **QUICK_STOP)
    config = write_shared_config(tmp_path, ["alpha", "beta", "gamma"])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(supervise(config, ["alpha", "beta", "gamma"], stop))
        try:
            assert await await_until(lambda: all(len(child_pids(markers[n])) >= 1 for n in markers)), \
                {n: child_pids(markers[n]) for n in markers}
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
    for name, marker in markers.items():
        assert len(child_pids(marker)) == 1, name


def test_crashing_instance_restarts_while_a_healthy_instance_keeps_running(tmp_path, monkeypatch):
    markers = redirect_children(monkeypatch, tmp_path, {"flaky": "exit", "steady": "hold"}, **FAST)
    config = write_shared_config(tmp_path, ["flaky", "steady"])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(supervise(config, ["flaky", "steady"], stop))
        try:
            assert await await_until(lambda: len(child_pids(markers["flaky"])) >= 3), \
                f"flaky did not restart: {child_pids(markers['flaky'])}"
            steady_pids = child_pids(markers["steady"])
            assert len(steady_pids) == 1, steady_pids
            assert await await_until(lambda: len(child_pids(markers["flaky"])) >= 5), \
                f"flaky stopped restarting: {child_pids(markers['flaky'])}"
            assert child_pids(markers["steady"]) == steady_pids
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())


@pytest.mark.parametrize("attempt,expected", [
    (1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 16.0), (6, 30.0), (7, 30.0), (20, 30.0),
])
def test_supervisor_restart_ladder_doubles_up_to_thirty_seconds(attempt, expected):
    """The approved ladder is 1, 2, 4 ... capped at 30 seconds and carries no jitter."""
    from src.main import supervisor_backoff_delay
    assert supervisor_backoff_delay(attempt) == expected


@pytest.mark.parametrize("attempt", [64, 1025, 10 ** 6, 2 ** 70])
def test_restart_ladder_stays_saturated_for_an_impossibly_long_crash_loop(attempt):
    """An instance that fails for days must still get the cap, never an OverflowError.

    The supervisor task computes this delay itself, so raising here would silently stop
    restarting that one instance.
    """
    from src.main import supervisor_backoff_delay
    assert supervisor_backoff_delay(attempt) == 30.0


def test_restart_ladder_is_independent_of_the_network_reconnect_backoff():
    """A reconnect backoff would add jitter and a 60 second cap; supervisor restarts must not use it."""
    from src.main import backoff_delay, supervisor_backoff_delay
    assert supervisor_backoff_delay(6) != backoff_delay(6)
    assert supervisor_backoff_delay(1) != backoff_delay(1)


def test_crashing_instance_restarts_with_a_growing_capped_delay(tmp_path, monkeypatch, capsys):
    """The delays a crash-looping instance actually waits grow and stop at the cap."""
    markers = redirect_children(monkeypatch, tmp_path, {"flaky": "exit"}, **FAST)
    config = write_shared_config(tmp_path, ["flaky"])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(supervise(config, ["flaky"], stop))
        try:
            assert await await_until(lambda: len(child_pids(markers["flaky"])) >= 6, timeout=20), \
                f"flaky did not restart: {child_pids(markers['flaky'])}"
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
    delays = retry_delays(capsys.readouterr().out, "flaky")
    assert delays[:5] == [0.1, 0.2, 0.4, 0.8, 0.8], delays


def test_a_stable_run_resets_the_restart_delay(tmp_path, monkeypatch, capsys):
    """An instance that held together for the reset window starts over at the first delay."""
    markers = redirect_children(monkeypatch, tmp_path, {"steady": ("stable", 0.3)}, **FAST,
                                SUPERVISOR_BACKOFF_RESET_SECONDS=0.2)
    config = write_shared_config(tmp_path, ["steady"])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(supervise(config, ["steady"], stop))
        try:
            assert await await_until(lambda: len(child_pids(markers["steady"])) >= 5, timeout=20), \
                f"steady did not restart: {child_pids(markers['steady'])}"
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=15)

    asyncio.run(scenario())
    delays = retry_delays(capsys.readouterr().out, "steady")
    assert len(delays) >= 4, delays
    assert set(delays) == {0.1}, delays


def test_a_child_that_cannot_start_backs_off_and_still_stops(tmp_path, monkeypatch, capsys):
    """A failed spawn is a restart attempt too: it waits, and a stop cuts the wait short."""
    config = write_shared_config(tmp_path, ["broken"])
    monkeypatch.setattr("src.main.agent_command",
                        lambda config_path, name: [str(tmp_path / "missing-interpreter")])
    monkeypatch.setattr("src.main.SUPERVISOR_BACKOFF_BASE_SECONDS", 5.0)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(supervise(config, ["broken"], stop))
        elapsed = None
        try:
            await asyncio.sleep(0.3)
            assert not stop.is_set()
        finally:
            stop.set()
            started = time.monotonic()
            await asyncio.wait_for(task, timeout=15)
            elapsed = time.monotonic() - started
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < 2.0, f"the 5 second backoff was not cut short: {elapsed:.1f}s"
    assert "could not start" in capsys.readouterr().out


def test_a_stop_during_child_creation_still_ends_that_child(tmp_path, monkeypatch):
    """The stop can land after the process exists but before the supervisor has registered it."""
    markers = redirect_children(monkeypatch, tmp_path, {"late": "hold"}, **QUICK_STOP)
    config = write_shared_config(tmp_path, ["late"])
    real_spawn = asyncio.create_subprocess_exec

    async def scenario():
        stop = asyncio.Event()

        async def spawn_then_stop(*argv, **kwargs):
            child = await real_spawn(*argv, **kwargs)
            # The supervisor is still inside create_subprocess_exec here.
            assert await await_until(lambda: len(child_pids(markers["late"])) >= 1, timeout=10)
            stop.set()
            return child

        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn_then_stop)
        task = asyncio.create_task(supervise(config, ["late"], stop))
        await asyncio.wait_for(task, timeout=15)
        return child_pids(markers["late"])

    pids = asyncio.run(scenario())
    assert pids, "the child never started, so nothing was exercised"
    for pid in pids:
        assert not process_alive(pid), f"child {pid} survived the stop"


def test_cancelling_the_supervisor_still_reclaims_its_children(tmp_path, monkeypatch):
    """Cancellation runs the same cleanup a stop signal does; no child may outlive it."""
    markers = redirect_children(monkeypatch, tmp_path, {"alpha": "hold", "beta": "hold"}, **QUICK_STOP)
    config = write_shared_config(tmp_path, ["alpha", "beta"])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(supervise(config, ["alpha", "beta"], stop))
        assert await await_until(lambda: all(len(child_pids(markers[n])) >= 1 for n in markers))
        pids = {name: child_pids(markers[name])[0] for name in markers}
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=15)
        return pids

    for name, pid in asyncio.run(scenario()).items():
        assert not process_alive(pid), f"{name} child {pid} survived the cancellation"


def test_stop_ends_every_child_and_stops_restarting(tmp_path, monkeypatch):
    markers = redirect_children(monkeypatch, tmp_path, {"flaky": "exit", "steady": "hold"}, **QUICK_STOP)
    config = write_shared_config(tmp_path, ["flaky", "steady"])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(supervise(config, ["flaky", "steady"], stop))
        assert await await_until(lambda: len(child_pids(markers["flaky"])) >= 2
                                 and len(child_pids(markers["steady"])) >= 1)
        stop.set()
        await asyncio.wait_for(task, timeout=15)
        started = child_pids(markers["flaky"])
        assert await await_until(lambda: not any(process_alive(pid) for pid in started)), started
        assert await await_until(lambda: not process_alive(child_pids(markers["steady"])[0]))
        settled = (len(child_pids(markers["flaky"])), len(child_pids(markers["steady"])))
        await asyncio.sleep(0.4)
        assert (len(child_pids(markers["flaky"])), len(child_pids(markers["steady"]))) == settled

    asyncio.run(scenario())


def test_stopping_several_leftover_children_costs_one_grace_period(tmp_path, monkeypatch):
    """The supervisor owns reclamation even when no instance task ends its own child.

    Terminating them one after another would spend N grace periods and blow the systemd stop
    budget, so the sweep has to run them together.
    """
    names = ("alpha", "beta", "gamma")
    markers = {name: tmp_path / f"{name}.marker" for name in names}
    config = write_shared_config(tmp_path, names)
    monkeypatch.setattr("src.main.SUPERVISOR_STOP_GRACE_SECONDS", 1.0)

    async def scenario():
        import src.main as mesh

        async def leave_the_child_behind(path, name, stop, children):
            """Stand in for an instance task that never terminates what it started."""
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-u", "-c", CHILD, "ignore", str(markers[name]), "3600")
            children[name] = child
            await stop.wait()

        monkeypatch.setattr("src.main._supervise_instance", leave_the_child_behind)
        stop = asyncio.Event()
        task = asyncio.create_task(mesh.run_agent_supervisor(config, list(names), stop))
        assert await await_until(lambda: all(len(child_pids(markers[n])) >= 1 for n in names))
        stop.set()
        started = time.monotonic()
        await asyncio.wait_for(task, timeout=20)
        return time.monotonic() - started, {n: child_pids(markers[n])[0] for n in names}

    elapsed, pids = asyncio.run(scenario())
    assert elapsed < 2.5, f"three grace periods were spent in sequence: {elapsed:.1f}s"
    for name, pid in pids.items():
        assert not process_alive(pid), f"{name} child {pid} survived"


def test_child_ignoring_sigterm_is_reclaimed_after_the_grace_period(tmp_path, monkeypatch):
    markers = redirect_children(monkeypatch, tmp_path, {"stubborn": "ignore"}, **QUICK_STOP)
    config = write_shared_config(tmp_path, ["stubborn"])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(supervise(config, ["stubborn"], stop))
        assert await await_until(lambda: len(child_pids(markers["stubborn"])) >= 1)
        pid = child_pids(markers["stubborn"])[0]
        assert process_alive(pid)
        stop.set()
        await asyncio.wait_for(task, timeout=15)
        assert not process_alive(pid)

    asyncio.run(scenario())


def test_the_shipped_grace_period_reclaims_a_child_that_ignores_sigterm(tmp_path, monkeypatch):
    """The deployed value, not a shortened stand-in, has to end a stubborn child."""
    import src.main as mesh
    assert mesh.SUPERVISOR_STOP_GRACE_SECONDS == 5.0
    markers = redirect_children(monkeypatch, tmp_path, {"stubborn": "ignore"})
    config = write_shared_config(tmp_path, ["stubborn"])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(supervise(config, ["stubborn"], stop))
        assert await await_until(lambda: len(child_pids(markers["stubborn"])) >= 1)
        pid = child_pids(markers["stubborn"])[0]
        stop.set()
        started = time.monotonic()
        await asyncio.wait_for(task, timeout=45)
        return time.monotonic() - started, pid

    elapsed, pid = asyncio.run(scenario())
    assert 4.5 <= elapsed < 20, f"the grace period was not honoured: {elapsed:.1f}s"
    assert not process_alive(pid)


def descendant_pids(config_path):
    """Every live interpreter still running this configuration's instances."""
    needle = str(Path(config_path).resolve()).encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if needle in cmdline and b"--agent-instance" in cmdline:
            found.append(int(entry.name))
    return found


def start_real_supervisor(config_path):
    return subprocess.Popen(
        [sys.executable, "-m", "src.main", "--mode", "agent",
         "--config", str(config_path)],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True)


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_supervisor_process_reaps_its_children_after_a_stop_signal(tmp_path, stop_signal):
    """The shipped entry point turns a stop signal into bounded reclamation, leaving no children."""
    config = write_shared_config(tmp_path, ["alpha", "beta"])
    proc = start_real_supervisor(config)
    try:
        assert wait_until(lambda: len(descendant_pids(config)) == 2), descendant_pids(config)
        proc.send_signal(stop_signal)
        stdout, _ = proc.communicate(timeout=30)
        assert proc.returncode == 0, stdout
        assert wait_until(lambda: descendant_pids(config) == []), descendant_pids(config)
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=10)
