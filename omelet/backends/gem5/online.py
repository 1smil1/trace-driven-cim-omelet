"""One persistent gem5 process for event-driven packet injection and delivery."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .adapter import prepare_gem5_process

# gem5 prints one "Entering event queue" line per idle fast-forward. An
# online run idles between injection batches, so the line is repeated
# millions of times and the raw stderr grows to hundreds of MB per run.
# The line carries no diagnostic information, so the drain keeps everything
# else verbatim and drops only that marker.
_IDLE_FASTFORWARD_MARKER = "Entering event queue"
_STDERR_TAIL_LINES = 2000

# Display-only milestones, in execution order. Unrecognized diagnostics keep
# the current milestone; they do not create additional simulation stages.
_INIT_STEPS = (
    ("initializing gem5", "creating traffic endpoints"),
    ("creating Ruby controllers", "Ruby controllers", "directory controllers"),
    ("Ruby controllers and topology created", "loading network configuration", "reading ", "read ", "creating link technology",
     "link technology", "preparing per-chiplet", "per-chiplet NoC", "creating NoC routers",
     "NoC routers", "NoI routers", "routers created", "intra-chiplet links",
     "creating NoC-NoI", "validating endpoint links", "network topology"),
    ("m5.instantiate:", "instantiate adopting object parameters"),
    ("instantiate resolving parameter proxies",),
    ("instantiate sorting objects", "instantiate writing config.ini",
     "instantiate collecting config.json", "instantiate writing JSON",
     "instantiate writing config.json", "instantiate writing DOT"),
    ("instantiate creating C++ objects",),
    ("instantiate connecting ports",),
    ("instantiate initializing objects and network routing",),
    ("instantiate binding statistics hierarchy", "statistics groups", "statistics allocation"),
    ("instantiate registering probe points",),
    ("instantiate registering probe listeners",),
    ("enabling statistics", "statistics visitor", "requestor statistic", "large statistic",
     "statistic allocation failed"),
    ("instantiate initializing object state", "instantiate loading checkpoint state"),
    ("instantiate completed", "m5.instantiate completed", "gem5 ready"),
)


class OnlineGem5Session:
    def __init__(self, command: list[str], outdir: str | Path, *,
                 log_dir: str | Path | None = None, debug: bool = False,
                 prefer_reuse_encoding: bool = False,
                 allow_cross_group: bool = False):
        if type(prefer_reuse_encoding) is not bool:
            raise TypeError("prefer_reuse_encoding must be a bool")
        if type(allow_cross_group) is not bool:
            raise TypeError("allow_cross_group must be a bool")
        if "--online-control" in command:
            raise ValueError("online-control is added by OnlineGem5Session")
        self.outdir = Path(outdir)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.log_dir = self.outdir if log_dir is None else Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.debug = debug
        self._started_at = time.monotonic()
        self._last_progress_at = self._started_at
        self._stage = "initializing gem5; waiting for handshake"
        self._init_step = 1
        self._operation = "handshake"
        self._tick = 0
        self._injected = 0
        self._arrived = 0
        self._scheduler_status = "scheduler not started"
        self._progress = (self.log_dir / "communication.log").open("w", encoding="utf-8", buffering=1)
        self._stdout_log = (self.log_dir / "gem5_stdout.log").open("w", encoding="utf-8", buffering=1)
        cmd, env = prepare_gem5_process([*command, "--online-control"])
        if env is None:
            env = os.environ.copy()
        # Online communication keeps every memory controller, but needs its
        # total memory counters rather than a controller x requestor table.
        env["OMELET_COMM_AGGREGATE_REQUESTORS"] = "1"
        # Keep equal-cost Garnet route choices stable for online workloads.
        # Route weights and physical network parameters remain unchanged.
        env["OMELET_DETERMINISTIC_ROUTES"] = "1"
        env["OMELET_COMM_DUMP_STATS"] = "1" if debug else "0"
        # Child-local policy; never rewrite shared workload/catalog files.
        env["OMELET_AUTO_REUSE_USE_ENCODING"] = "1" if prefer_reuse_encoding else "0"
        env["OMELET_AUTO_REUSE_ALLOW_CROSS_GROUP"] = "1" if allow_cross_group else "0"
        self._stderr_lines: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr = (self.log_dir / "gem5_stderr.log").open("w", encoding="utf-8")
        self._responses = queue.Queue()
        self.report_progress(force=True)
        try:
            self._process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
                errors="replace", env=env,
            )
            self._reader = threading.Thread(target=self._read_stdout, daemon=True)
            self._reader.start()
            self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
            self._stderr_reader.start()
            ready = self._receive()
            if ready != {"ready": True, "tick": 0}:
                raise RuntimeError(f"unexpected online gem5 handshake: {ready}")
            self._stage = "gem5 ready; waiting for workload scheduling"
            self.report_progress(force=True)
        except BaseException:
            self._stage = "failed during gem5 initialization"
            self.report_progress(force=True)
            if hasattr(self, "_process"):
                self._process.kill()
                self._process.wait()
            self._close_logs()
            raise

    def report_progress(self, status: str | None = None, *, force: bool = False) -> None:
        """Write a wall-clock heartbeat without issuing any simulator command."""
        if status is not None:
            self._scheduler_status = status
        now = time.monotonic()
        if not force and now - self._last_progress_at < 10:
            return
        self._last_progress_at = now
        step_label = ""
        if self._operation == "handshake":
            for step, prefixes in enumerate(_INIT_STEPS, 1):
                if self._stage.startswith(prefixes):
                    self._init_step = max(self._init_step, step)
            step_label = f"step={self._init_step}/{len(_INIT_STEPS)} "
        message = (
            f"{step_label}stage={self._stage} "
            f"wall_s={now - self._started_at:.1f} op={self._operation} "
            f"tick={self._tick} injected={self._injected} arrived={self._arrived} "
            f"pending={self._injected - self._arrived} {self._scheduler_status}\n"
        )
        self._progress.write(message)
        print(f"[Communication] method=7 {message}", end="", flush=True)

    def _read_stdout(self) -> None:
        try:
            for line in self._process.stdout:
                if line.startswith("OMELET_ONLINE "):
                    self._responses.put(line)
                else:
                    self._stdout_log.write(line)
                    if line.startswith("OMELET_INIT "):
                        self._stage = line.removeprefix("OMELET_INIT ").strip()
                        self.report_progress(force=True)
        except (OSError, ValueError) as exc:
            self._responses.put(exc)
        finally:
            self._responses.put(None)

    def _drain_stderr(self) -> None:
        """Keep gem5's stderr bounded and free of idle fast-forward spam.

        The full stderr is kept verbatim except for the idle fast-forward
        notice, which carries no diagnostic information and dominates the
        log size of a long online run.
        """
        stream = self._process.stderr
        if stream is None:
            return
        pending = ""
        while True:
            try:
                chunk = stream.read(1 << 20)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            pending += chunk
            lines = pending.split("\n")
            pending = lines.pop()
            for line in lines:
                if _IDLE_FASTFORWARD_MARKER in line:
                    continue
                self._stderr_lines.append(line)
                self._stderr.write(line + "\n")
                self._stderr.flush()
        if pending and _IDLE_FASTFORWARD_MARKER not in pending:
            self._stderr_lines.append(pending)
            self._stderr.write(pending + "\n")
            self._stderr.flush()

    def _close_logs(self) -> None:
        if hasattr(self, "_reader"):
            self._reader.join()
        if hasattr(self, "_stderr_reader"):
            self._stderr_reader.join(timeout=5)
        if hasattr(self, "_process") and self._process.stdout is not None:
            self._process.stdout.close()
        if hasattr(self, "_process") and self._process.stderr is not None:
            self._process.stderr.close()
        self._stderr.close()
        self._stdout_log.close()
        self._progress.close()

    def _receive(self) -> dict:
        while True:
            try:
                line = self._responses.get(timeout=10)
            except queue.Empty:
                self.report_progress(force=True)
                continue
            if line is None:
                break
            if isinstance(line, Exception):
                raise RuntimeError("failed reading gem5 online stdout") from line
            response = json.loads(line.removeprefix("OMELET_ONLINE "))
            if not isinstance(response, dict):
                raise RuntimeError("gem5 online response must be an object")
            self._tick = int(response["tick"])
            if "arrivals" in response:
                self._arrived += len(response["arrivals"])
            if self.debug:
                self._progress.write(f"DEBUG response {line}")
            return response
        self._stderr.flush()
        if hasattr(self, "_stderr_reader"):
            # The panic text is the last thing gem5 writes; wait for the
            # drain to flush it before reporting the tail.
            self._stderr_reader.join(timeout=10)
            self._stderr.flush()
        tail = (self.log_dir / "gem5_stderr.log").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[-20:]
        raise RuntimeError(
            f"gem5 online process ended (exit={self._process.poll()}):\n"
            + "\n".join(tail)
        )

    def _command(self, data: dict) -> dict:
        if self._process.poll() is not None:
            raise RuntimeError(f"gem5 online process exited: {self._process.returncode}")
        self._operation = data["op"]
        if self._operation == "run_cpp":
            self.report_progress(force=True)
        if self.debug:
            self._progress.write(f"DEBUG request {json.dumps(data)}\n")
        self._process.stdin.write(json.dumps(data) + "\n")
        self._process.stdin.flush()
        return self._receive()

    def inject(self, source: int, destination: int, size_bytes: int, packet_id: int) -> int:
        if any(type(value) is not int or value < 0 for value in
               (source, destination, packet_id)) or type(size_bytes) is not int or size_bytes <= 0:
            raise ValueError("online packet fields must be non-negative integers; bytes > 0")
        response = self._command({
            "op": "inject", "src": source, "dst": destination,
            "bytes": size_bytes, "id": packet_id,
        })
        self._injected += 1
        self._stage = "running workload"
        self.report_progress()
        return int(response["tick"])

    def advance(self, ticks: int) -> dict:
        if type(ticks) is not int or ticks <= 0:
            raise ValueError("advance ticks must be a positive integer")
        response = self._command({"op": "advance", "ticks": ticks})
        if not isinstance(response["arrivals"], list):
            raise RuntimeError("gem5 arrivals must be a list")
        return response

    def run_cpp(self, *, manifest: str, position: str, catalog: str,
                nonuniform_grid: str, endpoint_mode: str, k_node: int,
                k_chip: int, frequency_ghz: float, result_path: str) -> dict:
        """Run the complete online workload inside gem5's C++ controller."""
        fields = (manifest, position, catalog, nonuniform_grid, endpoint_mode,
                  result_path)
        if any(not isinstance(value, str) for value in fields):
            raise TypeError("C++ online workload paths and mode must be strings")
        if type(k_node) is not int or type(k_chip) is not int or k_node <= 0 or k_chip <= 0:
            raise ValueError("k_node and k_chip must be positive integers")
        if not isinstance(frequency_ghz, (int, float)) or frequency_ghz <= 0:
            raise ValueError("frequency_ghz must be positive")
        self._stage = "C++ online scheduler running"
        self._scheduler_status = "scheduler running; counters=last response"
        response = self._command({
            "op": "run_cpp", "manifest": manifest, "position": position,
            "catalog": catalog, "nonuniform_grid": nonuniform_grid,
            "endpoint_mode": endpoint_mode, "k_node": k_node,
            "k_chip": k_chip, "frequency_ghz": frequency_ghz,
            "result_path": result_path,
        })
        self._stage = "C++ online scheduler completed"
        self._scheduler_status = "scheduler completed"
        self._injected = int(response["injected"])
        self._arrived = int(response["arrived"])
        self.report_progress(force=True)
        return response

    def in_flight(self) -> int:
        return int(self._command({"op": "state"})["in_flight"])

    def close(self) -> Path | None:
        self._stage = (
            "finalizing gem5 statistics" if self.debug
            else "closing gem5; full statistics export disabled"
        )
        self.report_progress(force=True)
        if self.in_flight() != 0:
            raise RuntimeError("gem5 has packets in flight")
        response = self._command({"op": "quit"})
        if response["done"] is not True:
            raise RuntimeError("gem5 did not acknowledge quit")
        self._process.stdin.close()
        self._process.wait()
        if self._process.returncode != 0:
            raise RuntimeError(f"gem5 online process exited: {self._process.returncode}")
        stats = self.outdir / "stats.txt"
        if self.debug and (not stats.is_file() or stats.stat().st_size == 0):
            raise RuntimeError(f"gem5 produced no stats: {stats}")
        self._stage = "completed"
        self.report_progress(force=True)
        self._close_logs()
        return stats if self.debug else None

    def __enter__(self) -> OnlineGem5Session:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            try:
                self.close()
            except BaseException:
                self._stage = "failed during gem5 finalization"
                self.report_progress(force=True)
                if self._process.poll() is None:
                    self._process.kill()
                    self._process.wait()
                self._close_logs()
                raise
        else:
            self._stage = f"failed during workload: {exc_type.__name__}: {exc}"
            self.report_progress(force=True)
            self._process.kill()
            self._process.wait()
            self._close_logs()
