# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Jatin Bhanot <bhanot1054@gmail.com>

"""`build_modules()` — construct the L2-L8 modules for the phases built so far,
in dependency order (D-7 B6).

The orchestrator calls this in `initialize()` and drives the returned list
through `initialize -> start -> stop`; list order is start order
(L2 -> L3 -> ...). `bitnet_runtime` is passed for the modules that will need it
from B4 on.

L9 (`InterfaceLayer`, the socket/CLI/tray-facing "terminal accessibility"
surface) was removed by user decision: a text/CLI control surface will not be
maintained going forward, superseded by a future voice interface. Every
producer still publishes exactly what it always did (`MOMENT_PROPOSED`,
`ACTION_PROPOSAL`, `ACTION_CONFIRMATION_REQUEST`, `DMN_CYCLE_STARTED`/`_ENDED`,
the `BRIEFING_REQUEST`/`MIRROR_REQUEST`/`SYSTEM_HEALTH_REQUEST` bridges) —
none of that is "terminal", it is the general "ask without importing"
pattern (rules.md §0) any future interface reuses.

A3 (`Guardian`, below) is now the subscriber on the other end of
`MOMENT_PROPOSED`: it decides deliver/hold/drop and is the sole publisher of
the notification `ACTION_PROPOSAL` and of `MOMENT_DELIVERED`.
`NotificationDispatcher` turns a delivered moment into a real desktop
notification (`notify-send`) and is the sole publisher of `MOMENT_FEEDBACK`
(F2, VISION_PHASES.md). `presence` (`PresenceTracker`) remains the one
passive, socket-free module reporting through the health-dump file rather
than a live request, for `scripts/neuropaca_tray.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from neuropaca.action.executor import ActionExecutor
from neuropaca.agents.supervisor import AgentSupervisor
from neuropaca.core.base_module import BaseModule
from neuropaca.core.bitnet_runtime import BitNetRuntime
from neuropaca.core.clock import SystemClock
from neuropaca.core.config import Config
from neuropaca.core.episodes import EpisodeStore
from neuropaca.core.event_bus import EventBus
from neuropaca.core.graph_memory import GraphMemory
from neuropaca.core.presence_tracker import PresenceTracker
from neuropaca.diagnosis.correlator import SignalCorrelator
from neuropaca.drive.guardian import Guardian
from neuropaca.drive.pressure import PressureAccumulator
from neuropaca.idle.dmn import DefaultModeNetwork
from neuropaca.interface.moments import MomentComposer
from neuropaca.interface.notifier import NotificationDispatcher
from neuropaca.learning.plasticity import BitNetPlasticity
from neuropaca.learning.voice_command_parser import VoiceCommandParser
from neuropaca.learning.voice_intent_parser import VoiceIntentParser
from neuropaca.sensing.activity.collector import ActivityCollector
from neuropaca.sensing.collector_module import XMetricCollector
from neuropaca.sensing.collectors.filesystem import FileSystemCollector
from neuropaca.sensing.collectors.process import ProcessCollector
from neuropaca.sensing.collectors.system import SystemMetricCollector
from neuropaca.sensing.mail_ingest import MailIngest
from neuropaca.sensing.media_ingest import MediaIngest
from neuropaca.sensing.plugin_host import Plugin, PluginHost
from neuropaca.sensing.project_ingest import ProjectIngest
from neuropaca.sensing.raw_recorder import RawMetricsRecorder


def _ensure_repo_root_importable() -> None:
    """`plugins/` (S4 domain plugins) lives outside the installed `neuropaca`
    package — a normal `neuropaca daemon` process (not `python script.py`)
    doesn't get the repo root on `sys.path` for free, so importing
    `plugins.*` fails unless it's added explicitly. Idempotent; cheap enough
    to call unconditionally rather than duplicate the try/except per plugin."""
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def build_modules(
    config: Config,
    event_bus: EventBus,
    graph_memory: GraphMemory,
    bitnet_runtime: BitNetRuntime,
    episode_store: EpisodeStore | None = None,
) -> list[BaseModule]:
    # B2.5 (D-9): when the real Wayland ActivityCollector is on, XMetricCollector
    # stops emitting its CPU-derived IDLE_DETECTED / ACTIVITY_DETECTED stand-in.
    sensing = XMetricCollector(event_bus, config, emit_idle_from_cpu=not config.activity_enabled)
    sensing.register_collector(
        SystemMetricCollector(
            poll_interval_seconds=config.poll_intervals.get("system", 60.0),
            top_process_count=config.top_process_count,
        )
    )
    if config.watch_paths:
        sensing.register_collector(
            FileSystemCollector(
                watch_paths=config.watch_paths,
                ignore_globs=config.filesystem_ignore_globs,
                buffer_size=config.snapshot_buffer_size,
                poll_interval_seconds=config.poll_intervals.get("filesystem", 60.0),
            )
        )
    # B13-B2 (D-19): the per-process RAM/CPU/runtime census, grouped by app name.
    if config.process_collector_enabled:
        sensing.register_collector(
            ProcessCollector(
                poll_interval_seconds=config.poll_intervals.get("process", 60.0),
                min_rss_mb=config.process_min_rss_mb,
                exclude_names=config.process_exclude_names,
            )
        )

    diagnosis = SignalCorrelator(event_bus, config, graph_memory)
    learning = BitNetPlasticity(event_bus, config, graph_memory, bitnet_runtime)
    drive = PressureAccumulator(event_bus, config, graph_memory, clock=SystemClock())
    action = ActionExecutor(event_bus, config, graph_memory)
    agents = AgentSupervisor(event_bus, config, graph_memory, clock=SystemClock())
    idle_cognition = DefaultModeNetwork(
        event_bus,
        config,
        graph_memory,
        bitnet_runtime,
        clock=SystemClock(),
        episode_store=episode_store,
    )
    moments = MomentComposer(event_bus, config, graph_memory, clock=SystemClock())
    # A3 · the guardian (VISION.md §3.6). The sole subscriber of
    # `MOMENT_PROPOSED` from here on — `moments`/`MirrorComposer`/
    # `BriefingComposer` no longer deliver straight through. `episode_store`
    # is optional (like `idle_cognition`'s) — posteriors just don't survive a
    # restart without it.
    guardian = Guardian(event_bus, config, clock=SystemClock(), episode_store=episode_store)
    # F2 · turns a delivered moment into a real desktop notification and
    # captures the reaction (`notify-send --wait`, `interface/notifier.py`).
    notifier = NotificationDispatcher(event_bus, config, clock=SystemClock())
    # A1 · presence, minus L9 (RESEARCH_DOSSIER.md §21.20/21.21). No socket, no
    # write-back — just the state machine, reported through the normal
    # `health()` path so it lands in the periodic health-dump file
    # (`config.health_dump_path`) like every other module's counters.
    presence = PresenceTracker(event_bus, config, clock=SystemClock())

    # Start order = list order: L2 Sensing -> L3 Diagnosis -> L4 Learning ->
    # L5 Drive -> L7 Action -> L8 Agents -> L6 Idle Cognition -> A0 Moments —
    # the blueprint's own order (Architecture.md §10 A7, B7/B8) plus
    # VISION_PHASES.md's A0, minus the removed L9 Interface that used to sit
    # last. L5 sits after its two producers (L3, L4) and L7 immediately after
    # L5, so a threshold crossed during startup already has an executor
    # listening. L8 follows L7 for the same reason in reverse: it is the
    # second reader of that threshold, and it must not start before the layer
    # that will gate its `ACTION_PROPOSAL`s — A0's `MomentComposer` is a
    # second such proposer and follows the same rule. L4 and L6 share the loop
    # model and self-disable without llama-cpp-python / the model (D-11).
    # `presence` is last — like L9 before it, it only reads what every other
    # module already publishes.
    modules: list[BaseModule] = [sensing]
    # B13 · raw-data CSV. Passive METRIC_COLLECTED subscriber, appends one row
    # per reading. Right after sensing so it captures from the first poll.
    if config.raw_metrics_csv_path:
        modules.append(RawMetricsRecorder(event_bus, config))
    if config.activity_enabled:
        modules.append(ActivityCollector(event_bus, config))
    if config.mail_enabled:
        modules.append(MailIngest(event_bus, config, graph_memory, episode_store=episode_store))
    if config.project_tracking_enabled:
        modules.append(ProjectIngest(event_bus, config, graph_memory, episode_store=episode_store))
    if config.media_tracking_enabled:
        modules.append(MediaIngest(event_bus, config, graph_memory, episode_store=episode_store))

    domain_plugins: list[Plugin] = []
    if config.calendar_enabled or config.reading_enabled or config.voice_enabled:
        _ensure_repo_root_importable()
    if config.calendar_enabled:
        from plugins.calendar.calendar_plugin import CalendarPlugin

        domain_plugins.append(
            CalendarPlugin(
                calendar_path=config.calendar_ics_path,
                poll_interval=config.calendar_poll_interval_seconds,
            )
        )
    if config.reading_enabled:
        from plugins.reading.reading_plugin import ReadingListPlugin

        domain_plugins.append(
            ReadingListPlugin(
                reading_list_path=config.reading_list_path,
                poll_interval=config.reading_poll_interval_seconds,
            )
        )
    if config.voice_enabled:
        from neuropaca.diagnosis.app_identity import AppIdentity
        from plugins.voice.voice_plugin import VoicePlugin

        identity = (
            AppIdentity.from_file(config.app_identity_path)
            if config.app_identity_path
            else None
        )
        domain_plugins.append(
            VoicePlugin(
                utterances_path=config.voice_utterances_path,
                poll_interval=config.voice_poll_interval_seconds,
                identity=identity,
            )
        )
    if domain_plugins:
        modules.append(
            PluginHost(
                event_bus,
                config,
                graph_memory,
                episode_store=episode_store,
                plugins=domain_plugins,
                watermarks_path=getattr(config, "plugin_watermarks_path", None),
                name="domain_plugins",
            )
        )
    # A6.1 · classifies each voice utterance the PluginHost above just wrote
    # (`VOICE_UTTERANCE_CAPTURED`) — listed right after it for the same reason
    # L7 follows L5: whatever it reacts to must already have a listener up.
    if config.voice_enabled:
        modules.append(
            VoiceIntentParser(
                event_bus, config, graph_memory, bitnet_runtime, episode_store=episode_store
            )
        )
    # A6.2 · voice as hands (VISION_PHASES.md). Subscribes to VOICE_INTENT_CLASSIFIED
    # published by VoiceIntentParser above and proposes dangerous-tier actions —
    # each still runs through L7's confirmation handshake (rules.md §5.2).
    if config.voice_commands_enabled:
        modules.append(VoiceCommandParser(event_bus, config, graph_memory, bitnet_runtime))
    # A6.3 · speech in (VISION_PHASES.md). VoiceCaptureModule must already be
    # subscribed to VOICE_PTT_STARTED/_STOPPED before VoiceActivationModule
    # can publish either — same "listener before publisher" ordering as the
    # A6.1/A6.2 block above. Real backends only here (never Fake*): each one
    # self-disables cleanly if its optional dependency is missing (`stt_
    # backend.py`/`vad.py`/`voice_capture.py`'s own docstrings), the same
    # discipline `LlamaCppBackend` already follows — a dep-less CI runner
    # builds this module fine, it just never transcribes anything.
    if config.voice_speech_enabled:
        from neuropaca.interface.activation import VoiceActivationModule
        from neuropaca.sensing.stt_backend import FasterWhisperBackend, SttBackend
        from neuropaca.sensing.vad import SileroVadGate
        from neuropaca.sensing.voice_capture import SoundDeviceSource, VoiceCaptureModule

        local_stt = FasterWhisperBackend(
            config.voice_stt_model_size,
            language=config.voice_stt_language,
            n_threads=config.n_threads,
        )
        # "gemini" (user decision 2026-09-14): faster-whisper stays wired in
        # either way — as the wrapped fallback GeminiBridgeSttBackend falls
        # back to on any timeout/helper error, never a second, unused model.
        stt: SttBackend = local_stt
        if config.voice_stt_backend == "gemini":
            from neuropaca.sensing.cloud_voice_bridge import GeminiBridgeSttBackend

            stt = GeminiBridgeSttBackend(
                local_stt,
                bridge_dir=config.voice_cloud_bridge_dir,
                timeout_seconds=config.voice_cloud_timeout_seconds,
            )

        modules.append(
            VoiceCaptureModule(
                event_bus,
                config,
                SoundDeviceSource(),
                SileroVadGate(),
                stt,
            )
        )
        # "wake_word" and "both" are the only modes needing extra dependencies
        # (an always-on tap + a detector model) — "tray"/"hotkey" need nothing
        # beyond config, so those two args stay None otherwise.
        if config.voice_activation_mode in ("wake_word", "both"):
            from neuropaca.sensing.wake_word import OpenWakeWordDetector, SoundDeviceWakeWordSource

            modules.append(
                VoiceActivationModule(
                    event_bus,
                    config,
                    wake_word_detector=OpenWakeWordDetector(config.voice_wake_word_phrase),
                    wake_word_audio_source=SoundDeviceWakeWordSource(),
                )
            )
        else:
            modules.append(VoiceActivationModule(event_bus, config))

        # A6.2/rules.md §5.2 (user decision 2026-09-14): the "yes, confirm"
        # answerer for dangerous voice actions — see its own module docstring
        # for why this exists (the old CLI-based answerer was removed and
        # nothing replaced it) and why it never trusts a notification click.
        from neuropaca.interface.voice_confirmation import VoiceConfirmationBridge

        modules.append(VoiceConfirmationBridge(event_bus, config))
    modules.append(diagnosis)
    modules.append(learning)
    modules.append(drive)
    modules.append(action)
    if config.agents_enabled:
        modules.append(agents)
    modules.append(idle_cognition)
    if config.welcome_enabled:
        modules.append(moments)
    if config.guardian_enabled:
        modules.append(guardian)
        modules.append(notifier)
    modules.append(presence)
    return modules


# gen-ref: 79bf1837
