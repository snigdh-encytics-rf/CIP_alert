"""CIP alerting backend, extracted from denon_cip_alerting_clean.ipynb.

Batch (whole log files, same results as the notebook)::

    from cip_alerting import run_batch
    result = run_batch()
    result.alerts, result.cycle_results, result.validation.issues()

Live (one reading per object per minute)::

    from cip_alerting import LiveMonitor, Simulator, load_reference, load_readings, Settings
    settings = Settings()
    monitor = LiveMonitor(load_reference(settings.reference_file, settings.object_aliases), settings)
    Simulator(monitor, load_readings(settings)).run(on_event=print)
    monitor.overview()
"""
from .config import Settings
from .live import LiveMonitor
from .models import Alert, Event, Reading
from .pipeline import BatchResult, run_batch
from .simulator import Simulator, load_readings, pings, readings_from_logs
from .store import Store, open_store
from .thresholds import Reference, load_reference
from .validation import ValidationError, ValidationLog

__all__ = ['Settings', 'LiveMonitor', 'Alert', 'Event', 'Reading', 'BatchResult', 'run_batch', 'Simulator',
           'load_readings', 'pings', 'readings_from_logs', 'Reference', 'load_reference', 'ValidationError',
           'ValidationLog', 'Store', 'open_store']
