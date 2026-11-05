"""Device ingestion: validate readings pushed by plant gateways (``POST /api/readings``).

A reading uses the **original CIP log column names** (the same keys as one row of the station log, so a
gateway can forward the PLC tags unchanged and ``Reading.from_row`` builds it). Body: one reading object, or
``{"readings": [ ... ]}`` with at most ``MAX_READINGS_PER_REQUEST`` readings.

=========================  ==========================  ===========================================================
Field                      Type                        Rule
=========================  ==========================  ===========================================================
``Date Time``              string, required            ISO 8601 plant local time without a UTC offset,
                                                       e.g. ``"2026-07-24T09:50:12"`` (a space instead of ``T`` is
                                                       accepted)
``Line``                   string, required            CIP line, e.g. ``"M11W01"`` (1-64 characters)
``Object``                 string, required            Cleaned object, e.g. ``"C02C01-PMT TANK-1"`` (1-128)
``Recipe`` ``Activity``    string or null              As logged by the PLC (at most 128 characters). Unknown or
``Step``                                               missing values are accepted and reported as data checks.
``Time In Seconds``        number or null              Elapsed counter of the CIP run
``Temp SP`` ``Return Temp`` number or null             Set points and sensor values in PLC units (°C, L/hr, mS/cm)
``Supply Flow``
``Return Flow`` ``Min Return Flow SP``
``Return Conducivity`` (sic, as in the PLC)
``Caustic SP`` ``Acid SP`` ``Final Rinse Cond. SP`` ``Flip``
``Source File``            string or null              Optional trace back to a file (at most 256 characters)
``Excel Row``              integer >= 0 or null        Optional trace back to a row
=========================  ==========================  ===========================================================

Numbers must be finite JSON numbers (not strings, booleans, NaN or Infinity). Unknown fields are rejected, so a
misspelt tag never goes unnoticed. Readings of one object must arrive in time order: a reading at the same time
as, or earlier than, the last accepted reading of that object is rejected (``duplicate`` / ``out_of_order``).
"""
from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from .models import NUMERIC_FIELDS, TEXT_FIELDS, Reading

MAX_READINGS_PER_REQUEST = 5000
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_ERRORS = 200
TEXT_LIMITS = {'Line': 64, 'Object': 128, 'Recipe': 128, 'Activity': 128, 'Step': 128, 'Source File': 256}
REQUIRED_FIELDS = ('Date Time', 'Line', 'Object')
OPTIONAL_TEXT = ('Recipe', 'Activity', 'Step', 'Source File')
NUMERIC = tuple(NUMERIC_FIELDS)
ALL_FIELDS = (*REQUIRED_FIELDS, *OPTIONAL_TEXT[:3], *NUMERIC, 'Source File', 'Excel Row')
assert set(TEXT_FIELDS) <= set(ALL_FIELDS)


def reading_schema() -> dict:
    """JSON Schema (draft 2020-12) of one reading, served at GET /api/readings/schema."""
    nullable_text = lambda n: {'type': ['string', 'null'], 'maxLength': n}  # noqa: E731
    props = {
        'Date Time': {'type': 'string', 'format': 'date-time',
                      'description': 'Plant local time without a UTC offset, e.g. 2026-07-24T09:50:12'},
        'Line': {'type': 'string', 'minLength': 1, 'maxLength': TEXT_LIMITS['Line']},
        'Object': {'type': 'string', 'minLength': 1, 'maxLength': TEXT_LIMITS['Object']},
        **{name: nullable_text(TEXT_LIMITS[name]) for name in OPTIONAL_TEXT},
        **{name: {'type': ['number', 'null']} for name in NUMERIC},
        'Excel Row': {'type': ['integer', 'null'], 'minimum': 0},
    }
    reading = {'type': 'object', 'required': list(REQUIRED_FIELDS), 'additionalProperties': False,
               'properties': props}
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'CIP Monitor reading upload',
        'description': 'One reading, or {"readings": [...]} with at most '
                       f'{MAX_READINGS_PER_REQUEST} readings. Field names are the CIP log column names. '
                       'Readings of one object must be in time order.',
        'oneOf': [
            {'$ref': '#/$defs/reading'},
            {'type': 'object', 'required': ['readings'], 'additionalProperties': False,
             'properties': {'readings': {'type': 'array', 'minItems': 1, 'maxItems': MAX_READINGS_PER_REQUEST,
                                         'items': {'$ref': '#/$defs/reading'}}}},
        ],
        '$defs': {'reading': reading},
    }


class PayloadError(ValueError):
    """The body as a whole is unusable (400 with one message)."""


def _parse_time(value) -> tuple[pd.Timestamp | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, 'must be an ISO 8601 date-time string, e.g. "2026-07-24T09:50:12"'
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None, f'{value!r} is not an ISO 8601 date-time, e.g. "2026-07-24T09:50:12"'
    if parsed.tzinfo is not None:
        return None, 'must be plant local time without a UTC offset (no "Z" or "+01:00")'
    try:
        return pd.Timestamp(parsed), None
    except (ValueError, OverflowError):  # outside the supported range (years 1677-2262)
        return None, f'{value!r} is out of range'


def validate_reading(item, index: int) -> tuple[Reading | None, list[dict]]:
    errors: list[dict] = []

    def err(field, message):
        errors.append({'index': index, 'field': field, 'message': message})

    if not isinstance(item, dict):
        err(None, 'each reading must be a JSON object')
        return None, errors
    for name in item:
        if name not in ALL_FIELDS:
            err(name, 'unknown field; field names are the CIP log column names (see /api/readings/schema)')
    for name in REQUIRED_FIELDS:
        if item.get(name) is None:
            err(name, 'is required')
    timestamp = None
    if item.get('Date Time') is not None:
        timestamp, problem = _parse_time(item['Date Time'])
        if problem:
            err('Date Time', problem)
    for name, limit in TEXT_LIMITS.items():
        value = item.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            err(name, 'must be a string' + ('' if name in REQUIRED_FIELDS else ' or null'))
        elif name in REQUIRED_FIELDS and not value.strip():
            err(name, 'must not be empty')
        elif len(value) > limit:
            err(name, f'must be at most {limit} characters')
    for name in NUMERIC:
        value = item.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            err(name, 'must be a number or null')
        else:
            try:
                finite = math.isfinite(float(value))
            except OverflowError:
                finite = False
            if not finite:
                err(name, 'must be a finite number')
    row = item.get('Excel Row')
    if row is not None and (isinstance(row, bool) or not isinstance(row, int) or row < 0):
        err('Excel Row', 'must be a whole number >= 0 or null')
    if errors:
        return None, errors
    values = {key: item.get(key) for key in ALL_FIELDS if key != 'Date Time'}
    for name in ('Line', 'Object'):
        values[name] = values[name].strip()
    return Reading.from_row({**values, 'Date Time': timestamp}), []


def parse_payload(body) -> tuple[list[Reading], list[dict]]:
    """Body -> (readings, per-field errors). Raises PayloadError when the body shape itself is wrong."""
    if isinstance(body, dict) and 'readings' in body:
        extra = sorted(set(body) - {'readings'})
        if extra:
            raise PayloadError(f'unexpected top-level field(s) {extra}; send {{"readings": [...]}}')
        items = body['readings']
        if not isinstance(items, list):
            raise PayloadError('"readings" must be an array of reading objects')
    elif isinstance(body, dict):
        items = [body]
    else:
        raise PayloadError('body must be one reading object or {"readings": [...]}')
    if not items:
        raise PayloadError('"readings" is empty')
    if len(items) > MAX_READINGS_PER_REQUEST:
        raise PayloadError(f'too many readings ({len(items)}); send at most {MAX_READINGS_PER_REQUEST} per request')
    readings, errors = [], []
    for index, item in enumerate(items):
        reading, problems = validate_reading(item, index)
        if problems:
            errors.extend(problems)
        else:
            readings.append(reading)
    return readings, errors


def reading_payload(reading: Reading) -> dict:
    """A Reading as the JSON body field set (used by scripts/push_readings.py and the tests)."""
    data = reading.to_dict()
    data['Date Time'] = reading.timestamp.isoformat()
    if data.get('Excel Row') is None:
        data.pop('Excel Row', None)
    if data.get('Source File') is None:
        data.pop('Source File', None)
    return data
