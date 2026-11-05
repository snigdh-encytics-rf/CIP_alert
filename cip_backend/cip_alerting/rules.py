"""Alert rules (notebook section 5) and TACT parameter mapping (walkthrough).

- Duration alert: activity time > threshold x (1 + duration_tolerance).
- Flow monitoring: actual flow and the reference flow set point are kept for
  review, but flow does NOT create an alert.
- Cycle status: ALERT (at least one duration breach), OK, or NO REFERENCE
  (object not in the workbook, so not checked).

Both the batch pipeline and the live monitor call these functions, so the two
always apply the same rule.
"""
from __future__ import annotations

import math
from typing import Mapping

import numpy as np
import pandas as pd

from .thresholds import DURATION_THRESHOLDS, FLOW_THRESHOLDS, FLOWS

STATUS_OK = 'OK'
STATUS_ALERT = 'ALERT'
STATUS_NO_REFERENCE = 'NO REFERENCE'

DURATION_UNIT = 'minutes'
FLOW_UNIT = 'L/hr'


# ---------------------------------------------------------------------------
# Limits and breach tests (work on scalars and pandas Series alike)
# ---------------------------------------------------------------------------
def duration_limit(threshold, tolerance: float):
    return threshold * (1 + tolerance)


def flow_limit(threshold, tolerance: float):
    return threshold * (1 - tolerance)


def is_duration_breach(actual, threshold, tolerance: float):
    """True when actual minutes exceed threshold + tolerance. NaN never breaches."""
    return actual > duration_limit(threshold, tolerance)


def is_flow_below_reference(actual, threshold, tolerance: float):
    """Monitoring only: flow below its reference minus tolerance."""
    return actual < flow_limit(threshold, tolerance)


def cycle_status(has_reference: bool, alert_count: int) -> str:
    if not has_reference:
        return STATUS_NO_REFERENCE
    return STATUS_ALERT if alert_count > 0 else STATUS_OK


def alert_type(phase: str) -> str:
    return f'{phase} duration'


def phase_of(alert_type_name: str) -> str:
    return alert_type_name.removesuffix(' duration')


# ---------------------------------------------------------------------------
# Batch: apply the rules to every cycle
# ---------------------------------------------------------------------------
def apply_rules(cycle_results: pd.DataFrame, threshold_lookup: pd.DataFrame,
                duration_tolerance: float) -> pd.DataFrame:
    """Add threshold, breach, alert count and status columns to cycle_results."""
    for phase, threshold_col in DURATION_THRESHOLDS.items():
        cycle_results[f'{phase} Threshold (min)'] = cycle_results['Reference Object'].map(threshold_lookup[threshold_col])
        cycle_results[f'{phase} Breach'] = is_duration_breach(
            cycle_results[phase], cycle_results[f'{phase} Threshold (min)'], duration_tolerance)
    for flow in FLOWS:
        threshold_col = FLOW_THRESHOLDS[flow]
        cycle_results[threshold_col] = cycle_results['Reference Object'].map(threshold_lookup[threshold_col])

    # Flow values and set points remain available for monitoring, but do not create alerts.
    cycle_results['Alert Count'] = cycle_results[BREACH_COLUMNS].sum(axis=1)
    cycle_results['Status'] = np.select(
        [cycle_results['Reference Object'].isna(), cycle_results['Alert Count'] > 0],
        [STATUS_NO_REFERENCE, STATUS_ALERT], default=STATUS_OK)
    return cycle_results


BREACH_COLUMNS = [f'{phase} Breach' for phase in DURATION_THRESHOLDS]

# (alert type, breach column, actual column, threshold column, unit): duration only
ALERT_RULES = [
    (alert_type(phase), f'{phase} Breach', phase, f'{phase} Threshold (min)', DURATION_UNIT)
    for phase in DURATION_THRESHOLDS
]


# ---------------------------------------------------------------------------
# TACT parameters
# ---------------------------------------------------------------------------
# Per activity: (set point column, actual column, unit label) as used in the notebook walkthrough
TACT_PARAMETERS = {
    'Pre Rinse': [('Min Return Flow SP', 'Return Flow', 'Flow (L/hr)')],
    'Mid Rinse': [('Min Return Flow SP', 'Return Flow', 'Flow (L/hr)')],
    'Final Rinse': [('Min Return Flow SP', 'Return Flow', 'Flow (L/hr)')],
    'Caustic': [('Temp SP', 'Return Temp', 'Temperature'),
                ('Min Return Flow SP', 'Return Flow', 'Flow (L/hr)'),
                ('Caustic SP', 'Return Conducivity', 'Conductivity')],
    'Acid': [('Temp SP', 'Return Temp', 'Temperature'),
             ('Min Return Flow SP', 'Return Flow', 'Flow (L/hr)'),
             ('Acid SP', 'Return Conducivity', 'Conductivity')],
}

# Process measurement vs set point signals plotted in the walkthrough.
# (title, actual column, set point column, activity filter or None, y label)
PARAMETER_PLOTS = [
    ('Temperature', 'Return Temp', 'Temp SP', None, 'Temperature'),
    ('Return flow', 'Return Flow', 'Min Return Flow SP', None, 'Flow (L/hr)'),
    ('Caustic conductivity', 'Return Conducivity', 'Caustic SP', 'Caustic', 'Conductivity'),
    ('Acid conductivity', 'Return Conducivity', 'Acid SP', 'Acid', 'Conductivity'),
    ('Final-rinse conductivity', 'Return Conducivity', 'Final Rinse Cond. SP', 'Final Rinse', 'Conductivity'),
]

# The four TACT cells shown in the UI, mapped to log signals.
# Concentration's set point depends on the activity (see TACT_PARAMETERS).
TACT_SIGNALS = {
    'Temperature': {'label': 'Return temperature', 'actual': 'Return Temp', 'unit': '°C'},
    'Action': {'label': 'Return flow', 'actual': 'Return Flow', 'unit': 'L/hr'},
    'Concentration': {'label': 'Return conductivity', 'actual': 'Return Conducivity', 'unit': 'mS/cm'},
}

# Parameter status values. Only Time has an alert rule in the notebook; the
# other three are shown against their set point but are not evaluated.
STATUS_OUT_OF_LIMIT = 'out_of_limit'
STATUS_WITHIN_LIMIT = 'within_limit'
STATUS_NOT_EVALUATED = 'not_evaluated'    # value shown, no alert rule exists for it
STATUS_NOT_APPLICABLE = 'not_applicable'  # signal not a TACT parameter for this activity
STATUS_NO_DATA = 'no_data'                # sensor value missing; never shown as within limit


def _is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def tact_panel(values: Mapping[str, float], phase: str, duration_minutes: float,
               threshold_minutes: float, tolerance: float, proposed: Mapping[str, dict] | None = None) -> list[dict]:
    """The four TACT readings for one moment of an activity.

    ``values`` maps log column names (e.g. 'Return Temp', 'Temp SP') to numbers. ``proposed`` maps a TACT
    parameter to its proposed-rule evaluation (``ProposedTracker.view``); it is added as ``entry['proposed']``
    and never changes the official ``status`` (pending Danone sign-off).
    """
    in_phase = {actual: set_point for set_point, actual, _ in TACT_PARAMETERS.get(phase, [])}
    panel = []
    for parameter, signal in TACT_SIGNALS.items():
        actual_col = signal['actual']
        value = values.get(actual_col)
        set_point_col = in_phase.get(actual_col)
        set_point = values.get(set_point_col) if set_point_col else None
        if set_point_col is None:
            status = STATUS_NOT_APPLICABLE
        elif _is_missing(value):
            status = STATUS_NO_DATA
        else:
            status = STATUS_NOT_EVALUATED
        deviation = None if _is_missing(value) or _is_missing(set_point) else value - set_point
        panel.append({
            'parameter': parameter, 'label': signal['label'], 'source_column': actual_col,
            'value': None if _is_missing(value) else value, 'unit': signal['unit'],
            'set_point_column': set_point_col, 'set_point': None if _is_missing(set_point) else set_point,
            'limit': None, 'deviation': deviation, 'status': status,
        })
        if proposed and parameter in proposed and status != STATUS_NOT_APPLICABLE:
            panel[-1]['proposed'] = proposed[parameter]

    limit = duration_limit(threshold_minutes, tolerance)
    if _is_missing(duration_minutes):
        time_status = STATUS_NO_DATA
    elif _is_missing(threshold_minutes):
        time_status = STATUS_NOT_EVALUATED
    else:
        time_status = (STATUS_OUT_OF_LIMIT if is_duration_breach(duration_minutes, threshold_minutes, tolerance)
                       else STATUS_WITHIN_LIMIT)
    panel.append({
        'parameter': 'Time', 'label': f'{phase} duration', 'source_column': 'Time In Seconds',
        'value': None if _is_missing(duration_minutes) else duration_minutes, 'unit': 'min',
        'set_point_column': DURATION_THRESHOLDS.get(phase),
        'set_point': None if _is_missing(threshold_minutes) else threshold_minutes,
        'limit': None if _is_missing(limit) else limit,
        'deviation': (None if _is_missing(duration_minutes) or _is_missing(threshold_minutes)
                      else duration_minutes - threshold_minutes),
        'status': time_status,
    })
    return panel


def explain_duration_alert(phase: str, actual: float, threshold: float, tolerance: float) -> str:
    limit = duration_limit(threshold, tolerance)
    return (f'{phase} ran for {actual:.1f} min, above its {threshold:.1f} min threshold '
            f'plus {tolerance:.0%} tolerance ({limit:.1f} min).')


def rule_text(phase: str, tolerance: float) -> str:
    return f'{phase} duration > {phase} threshold x {1 + tolerance:g}'
