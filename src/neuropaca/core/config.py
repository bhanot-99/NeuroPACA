"""`Config` — loaded once at startup, immutable thereafter (Architecture.md §3.4).

`from_file()` reads TOML (`tomllib`, stdlib — no dependency). Every field is
validated in `__post_init__`; a bad value raises `ConfigError` and the daemon
refuses to start rather than run on a guess.

`inference_backend` (D-6): `"llama"` → the real llama.cpp backend, `"fake"` →
`FakeInferenceBackend` for tests. Validation is backend-aware — `model_path` is
only required to exist when the backend is `"llama"`.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from neuropaca.core.errors import ConfigError

_VALID_BACKENDS = frozenset({"llama", "fake"})


@dataclass(frozen=True, slots=True)
class Config:
    """Daemon configuration. Frozen — nothing mutates it after startup (rules.md §3.4).

    Defaults are chosen so `Config(inference_backend="fake")` is a usable test
    fixture with no config file.
    """

    model_path: str = ""
    graph_db_path: str = "data/graph.json"
    action_log_path: str = "data/actions.jsonl"
    idle_threshold_seconds: int = 300
    pressure_threshold: float = 1.0
    max_concurrent_agents: int = 2
    log_level: str = "INFO"
    poll_intervals: dict[str, float] = field(default_factory=lambda: {"system": 60.0})
    graph_save_interval_seconds: int = 300
    bitnet_max_tokens: int = 256
    inference_backend: str = "llama"
    # Concept variant (Architecture.md §3.4).
    n_threads: int = 4
    max_failures: int = 3  # consecutive collect() failures before a collector self-disables (D-7)
    max_file_tokens: int = 4096
    # B2 · Sensing (L2, D-7). poll_intervals keys are collector names ("system",
    # "filesystem"). watch_paths empty => FileSystemCollector stays disabled.
    snapshot_buffer_size: int = 720
    # B3 · Diagnosis (L3, D-8). Bounds SignalCorrelator's per-collector snapshot
    # deques: maxlen = ceil(correlation_window_seconds / poll_intervals[name]) + 1.
    correlation_window_seconds: int = 1800
    # B2.5 · Process & Activity Sensing (D-9). activity_enabled turns on the
    # Wayland ext-idle-notify ActivityCollector (needs `pip install .[activity]`);
    # when on, XMetricCollector stops emitting its CPU-derived idle stand-in.
    # top_process_count = top-N process-by-CPU rows in each system snapshot (0 = off).
    activity_enabled: bool = False
    top_process_count: int = 5
    watch_paths: list[str] = field(default_factory=list)
    filesystem_ignore_globs: list[str] = field(
        default_factory=lambda: [
            "*/.git/*",
            "*/node_modules/*",
            "*/__pycache__/*",
            "*/.venv/*",
            "*/.mypy_cache/*",
            "*/target/*",
            "*/dist/*",
            "*/build/*",
            "*.pyc",
        ]
    )

    def __post_init__(self) -> None:
        errs: list[str] = []

        if self.inference_backend not in _VALID_BACKENDS:
            errs.append(
                f"inference_backend must be one of {sorted(_VALID_BACKENDS)}, "
                f"got {self.inference_backend!r}"
            )
        if self.inference_backend == "llama":
            if not self.model_path:
                errs.append("model_path is required when inference_backend='llama'")
            elif not Path(self.model_path).is_file():
                errs.append(f"model_path does not exist: {self.model_path}")

        if self.log_level.upper() not in logging.getLevelNamesMapping():
            errs.append(f"unknown log_level: {self.log_level!r}")

        for name in (
            "idle_threshold_seconds",
            "graph_save_interval_seconds",
            "bitnet_max_tokens",
            "n_threads",
            "max_failures",
            "max_file_tokens",
            "snapshot_buffer_size",
            "correlation_window_seconds",
        ):
            if getattr(self, name) <= 0:
                errs.append(f"{name} must be > 0, got {getattr(self, name)}")

        if self.pressure_threshold <= 0:
            errs.append(f"pressure_threshold must be > 0, got {self.pressure_threshold}")
        if self.max_concurrent_agents < 0:
            errs.append(f"max_concurrent_agents must be >= 0, got {self.max_concurrent_agents}")
        if self.top_process_count < 0:
            errs.append(f"top_process_count must be >= 0, got {self.top_process_count}")

        for key, val in self.poll_intervals.items():
            if val <= 0:
                errs.append(f"poll_intervals[{key!r}] must be > 0, got {val}")

        if errs:
            raise ConfigError("invalid Config: " + "; ".join(errs))

    @classmethod
    def from_file(cls, path: str | Path) -> Config:
        """Load and validate a TOML config. Raises `ConfigError` on any problem."""
        p = Path(path)
        try:
            raw = tomllib.loads(p.read_text("utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read config file {p}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"malformed TOML in {p}: {exc}") from exc

        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(f"unknown config keys in {p}: {sorted(unknown)}")

        return cls(**raw)
