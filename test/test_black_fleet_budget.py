"""Fleet diagnostics must observe Black, never change its verdict or file scope."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
SCRIPT = ROOT / "scripts" / "check_black_formatting.py"


# A hosted fork runner's pre-step snapshot: 16 GB VM, no cgroup cap.
HOSTED_MEMINFO = "MemTotal:       16373444 kB\nMemAvailable:   14950508 kB\n"


def _step():
    steps = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["backend-lint"]["steps"]
    return next(step for step in steps if step.get("name") == "Check formatting (black, baselined)")


def _diagnostics():
    spec = importlib.util.spec_from_file_location(
        "ci_black_diagnostics", ROOT / "scripts" / "ci_black_diagnostics.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_does_not_run_gate_or_diagnostics(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("Import must not spawn children or collect resources")

    monkeypatch.setattr(subprocess, "run", unexpected)
    module = _diagnostics()
    assert callable(module.main)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("returncode", [0, 1, 123, -9, -11])
def test_diagnostics_run_gate_once_and_preserve_verdict(monkeypatch, capsys, returncode):
    diagnostics = _diagnostics()
    monkeypatch.setenv("BLACK_NUM_WORKERS", "2")
    phases = []
    diagnostics.snapshot = phases.append
    gate_calls = []

    def run(argv, **kwargs):
        gate_calls.append(argv)
        assert argv == [sys.executable, "scripts/check_black_formatting.py"]
        assert os.environ["BLACK_NUM_WORKERS"] == "2"
        assert kwargs == {}  # stdout/stderr stay inherited, not captured or suppressed.
        print("original black failure", file=sys.stderr)
        return subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(subprocess, "run", run)
    result = diagnostics.main("scripts/check_black_formatting.py")
    assert result == (returncode if returncode >= 0 else 128 - returncode)
    assert len(gate_calls) == 1
    assert phases == ["before", "after"]
    assert "original black failure" in capsys.readouterr().err


def test_failed_resource_probe_cannot_replace_gate_failure(monkeypatch, capsys):
    diagnostics = _diagnostics()
    monkeypatch.setattr(subprocess, "run", lambda argv: subprocess.CompletedProcess(argv, 123))

    def unavailable(phase):
        raise PermissionError("must not print exception payload")

    diagnostics.snapshot = unavailable
    assert diagnostics.main("scripts/check_black_formatting.py") == 123
    output = capsys.readouterr().out
    assert output.count("black resources unavailable: PermissionError") == 2
    assert "must not print exception payload" not in output


@pytest.mark.parametrize("version", [1, 2])
def test_resource_probe_reports_own_and_ancestor_cgroups_with_bounded_reads(capsys, version):
    diagnostics = _diagnostics()
    diagnostics.Path = PurePosixPath
    if version == 2:
        group = "0::/tenant/job\n"
        mount = "20 1 0:1 /tenant /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
        leaf = "memory.max"
        event = "memory.events"
        value = "oom_kill 1\n"
    else:
        group = "5:memory:/tenant/job\n"
        mount = "20 1 0:1 /tenant /sys/fs/cgroup rw - cgroup cgroup rw,memory\n"
        leaf = "memory.limit_in_bytes"
        event = "memory.oom_control"
        value = "oom_kill_disable 0\nunder_oom 0\noom_kill 1\n"
    files = {
        "/proc/meminfo": "MemTotal: 999999 kB\nMemAvailable: 888888 kB\n",
        "/proc/self/cgroup": group,
        "/proc/self/mountinfo": mount,
        f"/sys/fs/cgroup/job/{leaf}": "16384\n",
        f"/sys/fs/cgroup/{leaf}": "8192\n",
        f"/sys/fs/cgroup/job/{event}": value,
    }
    reads = []

    class BoundedFile(io.StringIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            return super().read(size)

    def fake_open(path, **kwargs):
        reads.append(str(path))
        if str(path) not in files:
            raise FileNotFoundError(path)
        return BoundedFile(files[str(path)])

    diagnostics.open = fake_open
    diagnostics.snapshot("before")
    output = capsys.readouterr().out
    assert f"cgroup /sys/fs/cgroup/job/{leaf}: 16384" in output
    assert f"cgroup /sys/fs/cgroup/{leaf}: 8192" in output
    assert "oom_kill 1" in output
    assert "host MemTotal: 999999 kB" in output
    assert "absent/hidden limits remain unknown" in output
    assert len(reads) < 25


@pytest.mark.parametrize("black_exit", [0, 1, 123, -9, -11])
@pytest.mark.parametrize("runner", ["github-hosted", "self-hosted", ""])
def test_gate_preserves_scope_workers_and_baseline_on_black_result(
    monkeypatch, tmp_path, black_exit, runner
):
    monkeypatch.setenv("RUNNER_ENVIRONMENT", runner)
    spec = importlib.util.spec_from_file_location("black_fleet_gate", SCRIPT)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    for target in gate.DEFAULT_TARGETS:
        (tmp_path / target).mkdir()
    # The prune path refuses unless the running black matches the `black==` pin it
    # reads from ROOT. This fake tree needs one, pinned to what is installed, so
    # the test still reaches the black-exit assertion it is about.
    (tmp_path / "pyproject.toml").write_text(
        f'"black=={importlib.metadata.version("black")}"\n', encoding="utf-8"
    )
    baseline = tmp_path / "baseline.txt"
    original = "# unchanged\nsrc/known.py\n"
    baseline.write_text(original, encoding="utf-8")
    monkeypatch.setenv("BLACK_NUM_WORKERS", "2")
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setattr(gate, "_read_text", lambda path: HOSTED_MEMINFO)
    calls = []

    def black_run(argv, **kwargs):
        calls.append(argv)
        # Every runner recycles; only hosted also sizes the pool to free memory.
        launcher = [str(ROOT / "scripts" / "bounded_black.py")]
        if runner == "github-hosted":
            launcher += ["--workers", "4"]
        assert argv == [
            sys.executable,
            *launcher,
            "--check",
            "--target-version",
            "py310",
            "src",
            "test",
        ]
        assert "env" not in kwargs  # Inherits BLACK_NUM_WORKERS, no override.
        assert os.environ["BLACK_NUM_WORKERS"] == "2"
        assert kwargs["cwd"] == tmp_path
        stderr = f"would reformat {tmp_path / 'src/known.py'}\n" if black_exit == 1 else "failure"
        return subprocess.CompletedProcess(argv, black_exit, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", black_run)
    if black_exit in (0, 1):
        expected = {"src/known.py"} if black_exit == 1 else set()
        assert gate._unformatted(gate.DEFAULT_TARGETS) == expected
    else:
        with pytest.raises(SystemExit) as error:
            gate.main(["--baseline", str(baseline), "--update-baseline"])
        assert error.value.code == 123
    assert baseline.read_text(encoding="utf-8") == original
    assert len(calls) == 1


def test_hidden_cgroup_ancestors_stay_inside_visible_mount(capsys):
    diagnostics = _diagnostics()
    diagnostics.Path = PurePosixPath
    paths = []

    def read(path, limit=4096):
        paths.append(str(path))
        return {
            "/proc/self/cgroup": "0::/../../hidden/job\n",
            "/proc/self/mountinfo": "20 1 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
            "/sys/fs/cgroup/memory.max": "8192",
        }.get(str(path), "")

    diagnostics.read = read
    diagnostics.snapshot("after")
    assert all(".." not in Path(path).parts for path in paths)
    assert "cgroup /sys/fs/cgroup/memory.max: 8192" in capsys.readouterr().out


def test_real_black_checks_both_targets_with_inherited_two_workers(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("black_fleet_real_gate", SCRIPT)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setenv("BLACK_NUM_WORKERS", "2")
    for target in gate.DEFAULT_TARGETS:
        directory = tmp_path / target
        directory.mkdir()
        (directory / "probe.py").write_text("value=1\n", encoding="utf-8")
    assert gate._unformatted(gate.DEFAULT_TARGETS) == {"src/probe.py", "test/probe.py"}
    # --check must not format either file, including an unbaselined target.
    for target in gate.DEFAULT_TARGETS:
        assert (tmp_path / target / "probe.py").read_text(encoding="utf-8") == "value=1\n"


@pytest.mark.parametrize("returncode", [0, 1, 123])
def test_script_entrypoint_preserves_gate_exit_and_output(monkeypatch, tmp_path, returncode):
    gate = tmp_path / "gate.py"
    gate.write_text(
        "import os, sys\n"
        "assert os.environ['BLACK_NUM_WORKERS'] == '2'\n"
        "print('gate ran once')\n"
        "print('gate stderr', file=sys.stderr)\n"
        f"sys.exit({returncode})\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BLACK_NUM_WORKERS", "2")
    # Only the current interpreter is needed, even with no binaries on PATH.
    monkeypatch.setenv("PATH", str(tmp_path))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ci_black_diagnostics.py"), str(gate)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == returncode
    assert result.stdout.count("gate ran once") == 1
    assert "gate stderr" in result.stderr
    assert "black resources before" in result.stdout
    assert "black resources after" in result.stdout
    assert f"black gate returncode={returncode}" in result.stdout


def test_executor_bounds_process_lifetime_and_parallelism(monkeypatch):
    launcher = _launcher()
    calls = []
    monkeypatch.setattr(launcher, "ProcessPoolExecutor", lambda **kwargs: calls.append(kwargs))
    for workers in (1, 2, 64):
        launcher.recycling_executor(workers)
        assert calls[-1]["max_workers"] == min(workers, launcher.MAX_WORKERS)
        assert calls[-1]["max_tasks_per_child"] == 1
        assert calls[-1]["mp_context"].get_start_method() == "spawn"


def test_floor_runs_the_exact_ci_black_command_and_worker_budget():
    import json

    profile = ROOT / "src/kiro_crew/builtin_skills/kirocrew-dev/prepare-pr/profiles/kirocrew.json"
    gates = json.loads(profile.read_text(encoding="utf-8"))["gates"]
    step = _step()
    assert "BLACK_NUM_WORKERS" not in step.get("env", {})
    command = f"BLACK_NUM_WORKERS={_launcher().MAX_WORKERS} {step['run']}"
    assert command in gates
    assert sum("scripts/check_black_formatting.py" in gate for gate in gates) == 1


@pytest.mark.parametrize(
    ("known", "new", "expected"),
    [
        ("value=1\n", "value = 1\n", 0),
        ("value=1\n", "value=2\n", 1),  # New offender must stay red.
        ("value = 1\n", "value = 2\n", 1),  # Graduate must still be pruned.
        ("value=1\n", "def broken(\n", None),  # Parse errors are not a verdict.
    ],
)
@pytest.mark.parametrize("runner", ["github-hosted", "self-hosted"])
def test_two_worker_real_gate_preserves_ratchet(
    monkeypatch, tmp_path, known, new, expected, runner
):
    from types import SimpleNamespace

    monkeypatch.setenv("RUNNER_ENVIRONMENT", runner)

    spec = importlib.util.spec_from_file_location("black_ratchet_real_gate", SCRIPT)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    if runner == "github-hosted":
        monkeypatch.delenv("BLACK_NUM_WORKERS", raising=False)
    else:
        monkeypatch.setenv("BLACK_NUM_WORKERS", "2")
    monkeypatch.setattr(
        gate, "_load_scope", lambda: SimpleNamespace(changed_paths=lambda: (None, "all"))
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "test").mkdir()
    (tmp_path / "src/known.py").write_text(known, encoding="utf-8")
    (tmp_path / "test/new.py").write_text(new, encoding="utf-8")
    baseline = tmp_path / "baseline.txt"
    original = "src/known.py\n"
    baseline.write_text(original, encoding="utf-8")
    if expected is None:
        with pytest.raises(SystemExit) as error:
            gate.main(["--baseline", str(baseline)])
        assert error.value.code == 123
    else:
        assert gate.main(["--baseline", str(baseline)]) == expected
    assert baseline.read_text(encoding="utf-8") == original
    assert (tmp_path / "src/known.py").read_text(encoding="utf-8") == known
    assert (tmp_path / "test/new.py").read_text(encoding="utf-8") == new


def _launcher():
    from scripts import bounded_black

    return bounded_black


@pytest.fixture(autouse=True)
def private_black_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("BLACK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("BLACK_NUM_WORKERS", "2")


def _black_run(tmp_path, args, native=False, driver=None):
    from installer_test_helpers import run_bounded

    prefix = ["-m", "black"] if native else [str(ROOT / "scripts/bounded_black.py")]
    if driver is not None:
        prefix = [str(driver)]
    return run_bounded(
        [sys.executable, *prefix, *args],
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "BLACK_NUM_WORKERS": os.environ.get("BLACK_NUM_WORKERS", "2"),
            "BLACK_CACHE_DIR": str(tmp_path / ("native-cache" if native else "bounded-cache")),
        },
        cwd=str(tmp_path),
        timeout=45,
    )


@pytest.mark.parametrize("singleton", [False, True])
@pytest.mark.parametrize(
    "content,code", [("value = 1\n", 0), ("value=1\n", 1), ("def bad(\n", 123)]
)
def test_native_cli_parity_cold_and_warm(tmp_path, singleton, content, code):
    directory = tmp_path / "src"
    directory.mkdir()
    (directory / "probe.py").write_text(content, encoding="utf-8")
    if not singleton:
        (directory / "second.pyi").write_text("value: int\n", encoding="utf-8")
    args = ["--check", "--target-version", "py310", "--verbose", "src"]
    for warm in (False, True):
        result = _black_run(tmp_path, args)
        native = _black_run(tmp_path, args, native=True)
        assert result.returncode == native.returncode == code, result.stderr
        assert result.stderr.splitlines()[-1] == native.stderr.splitlines()[-1]
        if warm and code == 0:
            assert "wasn't modified on disk since last run" in result.stderr
    assert (directory / "probe.py").read_text(encoding="utf-8") == content


def test_native_discovery_config_ignores_and_notebooks(tmp_path):
    import json

    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.black]\nline-length = 50\nforce-exclude = "blocked"\n', encoding="utf-8"
    )
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    directory = tmp_path / "src"
    directory.mkdir()
    for name in ("kept.py", "ignored.py", "blocked.py", "stub.pyi"):
        (directory / name).write_text("value=1\n", encoding="utf-8")
    nested = directory / "nested"
    nested.mkdir()
    (nested / ".gitignore").write_text("hidden.py\n", encoding="utf-8")
    (nested / "hidden.py").write_text("bad=1\n", encoding="utf-8")
    notebook = {
        "cells": [
            {
                "cell_type": "code",
                "metadata": {},
                "source": ["x=1"],
                "outputs": [],
                "execution_count": None,
            }
        ],
        "metadata": {"language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    (directory / "book.ipynb").write_text(json.dumps(notebook), encoding="utf-8")
    args = ["--check", "--no-cache", "--verbose", "src"]
    bounded = _black_run(tmp_path, args)
    native = _black_run(tmp_path, args, native=True)
    assert bounded.returncode == native.returncode == 1
    assert sorted(bounded.stderr.splitlines()) == sorted(native.stderr.splitlines())
    assert "would reformat" in bounded.stderr
    for name in ("ignored.py", "blocked.py", "hidden.py"):
        assert f"would reformat {directory / name}" not in bounded.stderr


@pytest.mark.parametrize("many", [False, True])
@pytest.mark.parametrize("failure", ["incomplete", "exception", "memory", "interrupt", "exit"])
def test_partial_findings_never_become_a_verdict(monkeypatch, tmp_path, many, failure):
    import black
    import black.concurrency as concurrency

    launcher = _launcher()
    monkeypatch.setattr(launcher, "limit_memory", lambda: None)
    for name in (["one.py", "two.py"] if many else ["one.py"]):
        (tmp_path / name).write_text("x=1\n", encoding="utf-8")

    def partial(**kwargs):
        if many:
            kwargs["report"].done(next(iter(kwargs["sources"])), black.Changed.YES)
        if failure != "incomplete":
            raise {
                "exception": RuntimeError,
                "memory": MemoryError,
                "interrupt": KeyboardInterrupt,
                "exit": SystemExit,
            }[failure](1)

    monkeypatch.setattr(
        concurrency if many else black, "reformat_many" if many else "reformat_one", partial
    )
    assert launcher.main(["--check", "--no-cache", str(tmp_path)]) == 123


@pytest.mark.parametrize("failure", [ImportError, NotImplementedError, OSError])
def test_pool_creation_never_falls_back_to_threads(monkeypatch, failure):
    launcher = _launcher()

    def refuse(**kwargs):
        raise failure("unavailable")

    monkeypatch.setattr(launcher, "ProcessPoolExecutor", refuse)
    with pytest.raises(RuntimeError, match="cannot create recycling"):
        launcher.recycling_executor(2)


def test_version_mismatch_fails_closed(monkeypatch):
    import black

    launcher = _launcher()
    monkeypatch.setattr(launcher, "limit_memory", lambda: None)
    monkeypatch.setattr(black, "__version__", "different")
    assert launcher.main(["--version"]) == 123


@pytest.mark.parametrize("failure", ["none", "exception", "death", "memory"])
def test_real_spawn_recycles_and_propagates_failures(tmp_path, monkeypatch, failure):
    if failure == "memory" and sys.platform != "linux":
        pytest.skip("RLIMIT_AS enforcement is Linux-only")
    monkeypatch.setenv("BLACK_NUM_WORKERS", "2")
    directory = tmp_path / "src"
    directory.mkdir()
    for index in range(24):
        (directory / f"{index:02}.py").write_text("value = 1\n", encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import os\nfrom pathlib import Path\n"
        "from scripts import bounded_black as launcher\n"
        "import black.concurrency as concurrency\n"
        "original = concurrency.format_file_in_place\n"
        "calls = 0\n"
        "def probe(src, *args):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    Path(src).with_suffix('.calls').write_text(str(calls))\n"
        f"    if Path(src).stem == '23' and {failure!r} != 'none':\n"
        f"        if {failure!r} == 'death': os._exit(7)\n"
        f"        if {failure!r} == 'memory': bytearray(launcher.MEMORY_BYTES)\n"
        "        raise RuntimeError('injected worker failure')\n"
        "    return original(src, *args)\n"
        "if __name__ == '__main__':\n"
        "    concurrency.format_file_in_place = probe\n"
        "    raise SystemExit(launcher.main())\n",
        encoding="utf-8",
    )
    result = _black_run(tmp_path, ["--check", "--no-cache", "--verbose", "src"], driver=driver)
    assert result.returncode == (0 if failure == "none" else 123), result.stderr
    if failure == "memory":
        assert "MemoryError" in result.stderr
        assert "injected worker failure" not in result.stderr
    # The OS may reuse a PID after a worker exits. Its task counter must still
    # start fresh; queued tasks may be cancelled after a real worker failure.
    counts = {path.stem: int(path.read_text()) for path in directory.glob("*.calls")}
    assert counts and set(counts.values()) == {1}
    if failure == "none":
        assert set(counts) == {f"{index:02}" for index in range(24)}
    else:
        assert "23" in counts, "the injected failure must actually execute"
    assert "resource_tracker: There appear to be" not in result.stderr


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("recycle", [True, False])
def test_pool_many_sequential_replacements_and_shutdown(tmp_path, workers, recycle):
    driver = tmp_path / "cycles.py"
    driver.write_text(
        "import multiprocessing\n"
        "from concurrent.futures import ProcessPoolExecutor\n"
        "from scripts.bounded_black import recycling_executor, limit_memory\n"
        "def pool_factory(workers):\n"
        f"    if {recycle!r}: return recycling_executor(workers)\n"
        "    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context('spawn'))\n"
        "calls = 0\n"
        "def probe():\n"
        "    global calls\n"
        "    calls += 1\n"
        "    return calls\n"
        "if __name__ == '__main__':\n"
        "    limit_memory()\n"
        f"    with pool_factory({workers}) as pool:\n"
        "        counts = [pool.submit(probe).result(timeout=10) for _ in range(32)]\n"
        "    assert counts == [1] * 32, counts\n"
        "    assert not multiprocessing.active_children()\n",
        encoding="utf-8",
    )
    result = _black_run(tmp_path, [], driver=driver)
    assert result.returncode == (0 if recycle else 1), result.stderr
    if not recycle:
        assert "AssertionError: [" in result.stderr


def test_native_cancellation_is_incomplete_and_drains_pool(tmp_path):
    directory = tmp_path / "src"
    directory.mkdir()
    for index in range(24):
        (directory / f"{index}.py").write_text("x=1\n", encoding="utf-8")
    driver = tmp_path / "cancel.py"
    driver.write_text(
        "import asyncio, multiprocessing, signal\n"
        "from scripts import bounded_black as launcher\n"
        "import black.concurrency as concurrency\n"
        "def event_loop():\n"
        "    loop = asyncio.new_event_loop()\n"
        "    def install(sig, callback, *args):\n"
        "        if sig == signal.SIGINT: loop.call_soon(callback, *args)\n"
        "    loop.add_signal_handler = install\n"
        "    return loop\n"
        "if __name__ == '__main__':\n"
        "    concurrency.maybe_use_uvloop = event_loop\n"
        "    result = launcher.main()\n"
        "    assert not multiprocessing.active_children()\n"
        "    raise SystemExit(result)\n",
        encoding="utf-8",
    )
    result = _black_run(tmp_path, ["--check", "--no-cache", "src"], driver=driver)
    assert result.returncode == 123, result.stderr
    assert "Aborted!" in result.stderr
    assert "incomplete Black report" in result.stderr
    assert "resource_tracker: There appear to be" not in result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Linux address-space ceiling")
@pytest.mark.parametrize("inherited", [(-1, -1), (1024**3, -1), (-1, 1024**3)])
def test_memory_limit_tightens_inherited_limits(monkeypatch, inherited):
    import resource

    launcher = _launcher()
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda kind: inherited)
    monkeypatch.setattr(resource, "setrlimit", lambda *args: calls.append(args))
    launcher.limit_memory()
    expected = min(value for value in (*inherited, launcher.MEMORY_BYTES) if value != -1)
    assert calls == [(resource.RLIMIT_AS, (expected, expected))]


def test_limit_setup_failure_is_internal_failure(monkeypatch):
    launcher = _launcher()

    def refuse():
        raise OSError("cannot enforce memory limit")

    monkeypatch.setattr(launcher, "limit_memory", refuse)
    assert launcher.main(["--version"]) == 123


def test_empty_native_discovery_and_invalid_cli(tmp_path):
    (tmp_path / "src").mkdir()
    for args, expected in [(["--check", "src"], 0), (["--unknown-option", "src"], 123)]:
        result = _black_run(tmp_path, args)
        assert result.returncode == expected, result.stderr


@pytest.mark.parametrize(
    ("meminfo", "expected"),
    [
        (HOSTED_MEMINFO, 6),  # 14.3 GiB free: six 2 GiB workers plus the coordinator.
        ("MemAvailable:    3145728 kB\n", 1),  # Tight VM still runs, one worker.
        ("MemAvailable:   33554432 kB\n", 15),
        ("MemTotal: 1 kB\n", None),
        ("MemAvailable: lots\n", None),
        ("MemAvailable: 1 MB\n", None),
        ("", None),
    ],
)
def test_memory_budget_fits_every_address_space_ceiling(meminfo, expected):
    launcher = _launcher()
    budget = launcher.memory_worker_budget(meminfo)
    assert budget == expected
    if expected is not None and expected > 1:
        available = int(meminfo.split()[1]) * 1024
        assert (budget + 1) * launcher.MEMORY_BYTES <= available


@pytest.mark.parametrize(
    ("cpus", "meminfo", "expected"),
    [
        (4, HOSTED_MEMINFO, 4),  # ubuntu-latest: CPU-bound, memory has headroom.
        (64, HOSTED_MEMINFO, 6),  # Memory, not CPUs, bounds a large VM.
        (64, "MemAvailable:   67108864 kB\n", 8),  # The launcher ceiling still holds.
        (4, "MemAvailable:    3145728 kB\n", 1),
        (4, "", 2),  # Unreadable meminfo never falls back to the CPU count.
        (None, HOSTED_MEMINFO, 1),
    ],
)
def test_hosted_worker_count_is_bounded(cpus, meminfo, expected):
    launcher = _launcher()
    assert launcher.hosted_worker_count(cpus, meminfo) == expected
    assert 1 <= expected <= launcher.MAX_WORKERS


def test_hosted_gate_never_runs_native_black(monkeypatch, tmp_path):
    # The native, default-worker command exhausts a hosted VM mid-walk.
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    spec = importlib.util.spec_from_file_location("black_hosted_gate", SCRIPT)
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setattr(gate, "_read_text", lambda path: HOSTED_MEMINFO)
    calls = []

    def black_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stderr="")

    monkeypatch.setattr(subprocess, "run", black_run)
    assert gate._unformatted(gate.DEFAULT_TARGETS) == set()
    (argv,) = calls
    assert "-m" not in argv
    assert argv[1] == str(ROOT / "scripts" / "bounded_black.py")
    assert argv[2:4] == ["--workers", "6"]


def test_sampler_records_memory_while_the_gate_runs(monkeypatch, tmp_path):
    # A killed runner never reaches the "after" snapshot, so samples must print
    # during the gate, flushed, from the diagnostic process itself.
    gate = tmp_path / "gate.py"
    gate.write_text("import time, sys\ntime.sleep(1.5)\nsys.exit(1)\n", encoding="utf-8")
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('d', {str(ROOT / 'scripts' / 'ci_black_diagnostics.py')!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "module.SAMPLE_SECONDS = 0.2\n"
        "raise SystemExit(module.main(sys.argv[1]))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(driver), str(gate)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 1
    samples = [line for line in result.stdout.splitlines() if "black memory sample t=" in line]
    assert len(samples) >= 2, result.stdout
    if sys.platform == "linux":
        assert "MemAvailable=?" not in samples[0]
    # Sampling stops with the gate: nothing prints after its verdict line.
    tail = result.stdout.split("black gate returncode=1")[1]
    assert "black memory sample" not in tail


def test_sampler_failure_cannot_change_the_verdict(monkeypatch, capsys):
    diagnostics = _diagnostics()
    diagnostics.snapshot = lambda phase: None

    def broken(elapsed):
        raise PermissionError("must not print exception payload")

    diagnostics.sample = broken
    diagnostics.SAMPLE_SECONDS = 0.05

    def run(argv, **kwargs):
        import time

        time.sleep(0.3)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(subprocess, "run", run)
    assert diagnostics.main("scripts/check_black_formatting.py") == 1
    output = capsys.readouterr().out
    assert "black memory sample unavailable: PermissionError" in output
    assert "must not print exception payload" not in output


def test_own_memory_current_reads_the_process_cgroup():
    diagnostics = _diagnostics()
    diagnostics.Path = PurePosixPath
    diagnostics.read = lambda path, limit=4096: {
        "/proc/self/cgroup": "0::/system.slice/runner.service\n",
        "/proc/self/mountinfo": "20 1 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
        "/sys/fs/cgroup/system.slice/runner.service/memory.current": "123456\n",
    }.get(str(path), "")
    assert diagnostics.own_memory_current() == "123456"
    diagnostics.read = lambda path, limit=4096: ""
    assert diagnostics.own_memory_current() == ""
