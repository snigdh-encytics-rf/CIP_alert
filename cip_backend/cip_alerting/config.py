"""Settings for the CIP alerting backend.

Values come from section 1 ("Settings") of denon_cip_alerting_clean.ipynb.
Change them here, or pass a modified ``Settings`` to ``run_batch`` / ``LiveMonitor``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# cip_backend/cip_alerting/config.py -> project folder that holds datasets/
PROJECT_DIR = Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    """$CIP_DATA_DIR if set (e.g. /data in Docker), else <project>/datasets."""
    env = os.environ.get('CIP_DATA_DIR', '').strip()
    return Path(env) if env else PROJECT_DIR / 'datasets'


ALIASES_FILE = Path(__file__).resolve().parents[1] / 'rules' / 'object_aliases.json'


def default_object_aliases() -> Path | None:
    """$CIP_OBJECT_ALIASES: 'off' disables the name mapping (notebook numbers), a path picks another file,
    empty uses rules/object_aliases.json."""
    env = os.environ.get('CIP_OBJECT_ALIASES', '').strip()
    if env.lower() in ('off', '0', 'false', 'no', 'none'):
        return None
    return Path(env) if env else ALIASES_FILE


def default_proposed_rules() -> str:
    """$CIP_PROPOSED_RULES: off, show (default) or alert. See proposed_rules.py."""
    return os.environ.get('CIP_PROPOSED_RULES', '').strip().lower() or 'show'

# Limits used by the validation checks
ACTIVITIES = ('Pre Rinse', 'Caustic', 'Mid Rinse', 'Acid', 'Final Rinse', 'Final Drain')
EXPECTED_RECIPES = frozenset({'Caustic', 'Caustic Acid', 'Acid', 'Final rinse'})
EXPECTED_STEPS = frozenset({'Fill To Tank', 'Fill Circuit', 'Front Mix Zone', 'Circulation',
                            'Empty Tank', 'Heating', 'Start Next Activity'})
VALID_RANGES = {  # physically plausible sensor ranges
    'Return Temp': (-5, 120),
    'Return Conducivity': (0, 250),
    'Supply Flow': (0, 60000),
    'Return Flow': (0, 60000),
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    log_pattern: str = 'CIP_Log*.xlsx'
    reference_file_name: str = 'CIP Pre Rinse Timer details.xlsx'

    cycle_gap_minutes: float = 30      # a longer gap between records starts a new cycle
    duration_tolerance: float = 0.10   # alert when actual duration > threshold + 10%
    flow_tolerance: float = 0.10       # monitoring guide only; flow does not create alerts

    sample_interval_seconds: int = 60  # devices report once a minute
    min_cycle_records: int = 5
    max_cycle_hours: float = 4

    # Proposed TACT rules (rules/proposed_tact_rules.json), pending Danone sign-off.
    # 'show' = debug panel only; 'alert' = they also raise alerts (changes alert counts: never the default).
    object_aliases_file: Path | None = field(default_factory=default_object_aliases)
    proposed_rules: str = field(default_factory=default_proposed_rules)
    proposed_rules_file: Path | None = None   # None = $CIP_PROPOSED_RULES_FILE or rules/proposed_tact_rules.json

    @property
    def log_files(self) -> list[Path]:
        return sorted(Path(self.data_dir).glob(self.log_pattern))

    @property
    def object_aliases(self) -> dict[str, str]:
        """Log object -> workbook object, for objects the equipment code cannot match (empty when off)."""
        if self.object_aliases_file is None:
            return {}
        path = Path(self.object_aliases_file)
        if not path.exists():
            raise FileNotFoundError(f'Object alias file not found: {path} (set CIP_OBJECT_ALIASES=off to skip it)')
        import json
        aliases = json.loads(path.read_text()).get('aliases', {})
        if not isinstance(aliases, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in aliases.items()):
            raise ValueError(f'{path}: "aliases" must map log object names to workbook object names')
        return {k.strip(): v.strip() for k, v in aliases.items()}

    @property
    def reference_file(self) -> Path:
        return Path(self.data_dir) / self.reference_file_name
