"""Replay the CIP log files as if each object were an IoT device pinging every 60 seconds.

Readings are released in time order, grouped into one "ping" per minute. Each
ping is fed to a ``LiveMonitor``; idle cycles are closed after every ping.
Swap ``readings_from_logs`` for a real feed (MQTT, OPC UA, HTTP) later; the
monitor only needs ``Reading`` objects.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator

import pandas as pd

from .config import Settings
from .live import LiveMonitor
from .loader import load_logs
from .models import Event, Reading


def readings_from_logs(logs: pd.DataFrame) -> list[Reading]:
    """Every log record as a Reading, oldest first."""
    ordered = logs.sort_values(['Date Time', 'Line', 'Object'], kind='stable')
    return [Reading.from_row(row) for row in ordered.to_dict('records')]


def load_readings(settings: Settings | None = None) -> list[Reading]:
    settings = settings or Settings()
    logs, _ = load_logs(settings.log_files)
    return readings_from_logs(logs)


def pings(readings: Iterable[Reading], interval_seconds: int = 60) -> Iterator[tuple[pd.Timestamp, list[Reading]]]:
    """Group time-ordered readings into one batch per interval."""
    batch: list[Reading] = []
    slot = None
    freq = f'{interval_seconds}s'
    for reading in readings:
        reading_slot = reading.timestamp.floor(freq)
        if slot is not None and reading_slot != slot:
            yield slot, batch
            batch = []
        slot = reading_slot
        batch.append(reading)
    if batch:
        yield slot, batch


class Simulator:
    """Feed readings to a monitor ping by ping.

    speed: None = as fast as possible; 1 = real time (60 s per ping); 60 = one ping per second.
    """

    def __init__(self, monitor: LiveMonitor, readings: Iterable[Reading],
                 interval_seconds: int = 60, speed: float | None = None):
        self.monitor = monitor
        self.readings = readings
        self.interval_seconds = interval_seconds
        self.speed = speed

    def run(self, on_event: Callable[[Event], None] | None = None, until: pd.Timestamp | None = None,
            max_pings: int | None = None, flush: bool = False) -> list[Event]:
        events: list[Event] = []

        def emit(new_events):
            events.extend(new_events)
            if on_event:
                for event in new_events:
                    on_event(event)

        for count, (slot, batch) in enumerate(pings(self.readings, self.interval_seconds), start=1):
            if until is not None and slot > until:
                break
            for reading in batch:
                emit(self.monitor.ingest(reading))
            emit(self.monitor.close_idle(slot + pd.Timedelta(seconds=self.interval_seconds)))
            if max_pings is not None and count >= max_pings:
                break
            if self.speed:
                time.sleep(self.interval_seconds / self.speed)
        if flush:
            emit(self.monitor.flush())
        return events
