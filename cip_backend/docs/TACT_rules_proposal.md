# Proposed alert rules for Temperature, Action and Concentration

**Status: Proposed — pending Danone sign-off.** None of these rules is approved. They were derived from the CIP log data and need review by Danone process and quality engineers.

**For:** Danone process and quality engineers, Ambala plant
**Data:** CIP logs from 1 to 26 July 2026: 4 CIP lines, 38,943 one-minute readings and 699 cycles. Only aggregate statistics are shown.
**Rule file:** `rules/proposed_tact_rules.json`. To reproduce the numbers, run `CIP_OBJECT_ALIASES=off python scripts/run_batch.py`.
**Scope:** the figures cover the 22 objects matched to the workbook by equipment code (the notebook's basis, 221 Time alerts). The monitor now also matches 7 more objects by name (`rules/object_aliases.json`); with them the Time rule raises 561 alerts and the proposed rules project 68 instead of 47. Those 7 mappings are themselves awaiting Danone's confirmation.

## 1. Why this is needed

Today the CIP Monitor raises alerts only on **Time**: an activity that runs more than 10% over its reference duration. Over July that rule raised 221 alerts.

Temperature, flow and conductivity are shown against their PLC set points, but they never raise an alert, because no limit has been agreed for them.

This document proposes one simple rule per TACT parameter and activity, with thresholds chosen from the July data. For each rule it shows what that rule would have raised over July.

## 2. How every rule works

> **[Signal] below [limit] for [N] minutes or longer, during [activity] circulation, ignoring the first [G] minutes of circulation.**

- **Limit.** It is derived from the PLC set point in the same reading. Temperature uses set point − X °C; flow and conductivity use Y% of set point. A different set point per object or per run is therefore handled automatically.
- **When the rule is checked.**
  - Only during the **Circulation** step of the activity, where the set point is meant to hold.
  - The first **G** minutes of each circulation block are skipped, to allow for ramp-up.
- **What counts toward the N minutes.**
  - Only consecutive readings below the limit count.
  - A streak ends on any of the following: a reading at or above the limit, a missing reading, a reading outside the plausible sensor range, or a change of step. Negative flow readings are outside the plausible range.
- **How often it alerts.** At most **one alert per rule per cycle**, raised at the first minute the streak reaches N minutes.
- **Which objects it covers.** Only objects that have reference thresholds, which are the same objects the Time rule monitors.
- **Set point columns used.**
  - Temperature: `Return Temp` vs `Temp SP`.
  - Flow: `Return Flow` vs `Min Return Flow SP`.
  - Conductivity: `Return Conducivity` vs `Caustic SP` or `Acid SP`.

The thresholds follow two choices:

- **10% tolerance.** Flow and conductivity use 90% of set point. This matches the 10% the monitor already uses for time and for flow monitoring.
- **5 °C for temperature.** This is a round margin above the ramp-up behaviour seen in the data.

The rinse rules use shorter timings, because rinse circulation lasts only 2–5 minutes.

## 3. Summary

| Rule | Checks | Proposed thresholds | Projected alerts, July | Cycles checked | Also had a Time alert* |
|---|---|---|---:|---:|---:|
| P-T-CAUSTIC | Caustic wash temperature | > 5 °C below SP for ≥ 5 min (first 5 min not checked) | 7 | 481 | 7 |
| P-T-ACID | Acid wash temperature | > 5 °C below SP for ≥ 5 min (first 5 min not checked) | 4 | 61 | 4 |
| P-C-CAUSTIC | Caustic conductivity | < 90% of SP for ≥ 5 min (first 5 min not checked) | 5 | 481 | 5 |
| P-C-ACID | Acid conductivity | < 90% of SP for ≥ 5 min (first 5 min not checked) | 5 | 61 | 5 |
| P-A-CAUSTIC | Caustic wash return flow | < 90% of SP for ≥ 5 min (first 5 min not checked) | 14 | 481 | 14 |
| P-A-ACID | Acid wash return flow | < 90% of SP for ≥ 5 min (first 5 min not checked) | 0 | 61 | – |
| P-A-PRE-RINSE | Pre-rinse return flow | < 90% of SP for ≥ 3 min (first 2 min not checked) | 5 | 483 | 5 |
| P-A-MID-RINSE | Mid rinse return flow | < 90% of SP for ≥ 3 min (first 2 min not checked) | 1 | 61 | n/a (no time limit) |
| P-A-FINAL-RINSE | Final rinse return flow | < 90% of SP for ≥ 3 min (first 2 min not checked) | 6 | 484 | 5 |
| **Total** | | | **47** | | **45 of 46** |

\* "Also had a Time alert" means the same activity in the same cycle also ran over its time limit.

All 47 projected alerts fall on lines M11W01 (33) and M11W02 (14). They come from 37 cycles on 11 objects, against 221 Time alerts over the same period.

**The main finding.** In 45 of the 46 cases where the activity has a time limit, a low temperature, flow or conductivity occurred in a cycle where that activity also overran its time. This is consistent with the PLC extending a step until its conditions are met. If so, these rules would point to the likely cause of many Time alerts. The data shows the overlap but does not prove the cause.

## 4. The rules

All percentages below are shares of circulation readings after the ramp-up allowance, for objects with reference thresholds. "P10" means 10% of readings are lower than the value given.

### P-T-CAUSTIC: caustic wash temperature low

**What it checks:** return temperature more than **5 °C** below `Temp SP` for **5 min** or longer during caustic circulation. The first 5 min are not checked.

**Evidence** (481 cycles, 8,781 circulation readings, set point 80 °C in every reading):

- **Ramp-up.** The set point is reached after a median of 1 min. 90% of runs reach it within 3 min and 95% within 7 min.
- **Position against set point.** After 5 min, readings sit a median of **+9.9 °C** above the set point; P10 is +2.8 °C and P5 is −1.8 °C.
- **Below the limit.** 5.8% of readings are below the set point and 3.5% are more than 5 °C below it.

**Projected:** 7 alerts on 6 objects, which is 1.5% of caustic cycles. All 7 coincide with a caustic Time alert. The temperature stayed below the limit for a median of 19 min (longest 44 min).

**Sensitivity** (alerts by margin and streak length):

| Margin | 3 min | 5 min | 10 min |
|---|---:|---:|---:|
| 2 °C | 20 | 12 | 7 |
| **5 °C** | 15 | **7** | 7 |
| 10 °C | 7 | 4 | 3 |

**Open questions:**

- Is 80 °C a minimum that must be held, or a target?
- Should the temperature be judged on return or on supply temperature?
- Is 5 °C for 5 min acceptable, or is any time below 80 °C a quality deviation?

### P-T-ACID: acid wash temperature low

**What it checks:** the same rule applied to acid circulation (set point 65 °C).

**Evidence** (61 cycles, 1,228 readings):

- **Ramp-up.** The set point is reached after a median of 1 min; 90% of runs reach it within 4 min.
- **Position against set point.** After 5 min, the median reading is **+15.4 °C** above the set point; P10 is −4.2 °C.
- **Below the limit.** 12.4% of readings are below the set point and 9.1% are more than 5 °C below it.

**Projected:** 4 alerts on 3 objects, which is 6.6% of acid cycles. All 4 coincide with an acid Time alert; the median time below the limit was 14.5 min.

**Sensitivity:** every combination of 2–10 °C and 3–10 min gives 3–4 alerts. The same cycles are always caught, so the exact threshold matters little here.

**Open question:** is one temperature rule for both chemical washes acceptable, or should acid have its own margin?

### P-C-CAUSTIC: caustic concentration low

**What it checks:** return conductivity below **90%** of `Caustic SP` (55 mS/cm, so below 49.5 mS/cm) for **5 min** or longer during caustic circulation. The first 5 min are not checked.

**Evidence** (481 cycles):

- **Ramp-up.** Conductivity is above the set point from the first minute in 90% of runs.
- **Position against set point.** After 5 min, the median reading is **164%** of the set point; P10 is 107% and P5 is 101%.
- **Below the limit.** 4.2% of readings are below the set point and 1.8% are below 90% of it.

**Projected:** 5 alerts on 3 objects, which is 1.0% of caustic cycles. All 5 coincide with a caustic Time alert; the median time below the limit was 9 min.

**Sensitivity:**

| Limit | 3 min | 5 min | 10 min |
|---|---:|---:|---:|
| 100% | 9 | 8 | 4 |
| **90%** | 7 | **5** | 2 |
| 80% | 5 | 4 | 2 |

**Open questions:**

- Does 55 mS/cm correspond to the minimum caustic concentration required, and at what temperature?
- Can Danone give the conductivity-to-concentration (%) conversion, so the panel can show concentration directly?

### P-C-ACID: acid concentration low

**What it checks:** return conductivity below **90%** of `Acid SP` (65 mS/cm, so below 58.5 mS/cm) for **5 min** or longer during acid circulation. The first 5 min are not checked.

**Evidence** (61 cycles):

- **Acid behaves differently from caustic.** After 5 min, the median reading is **99%** of the set point; P25 is 85% and P10 is 56%.
- **Below the limit.** **52%** of readings are below the set point and 32% are below 90% of it.

A "below set point" rule would therefore fire on most acid runs.

**Projected:** 5 alerts on 4 objects, which is 8.2% of acid cycles. All 5 coincide with an acid Time alert. These are long episodes: the median time below the limit was 62 min.

**Sensitivity:** at 100% of set point the rule gives 8–10 alerts, at 90% it gives 5, and at 80% it gives 3. These counts hold for streaks of 3–10 min.

**Open question:** the acid conductivity set point (65) equals the acid temperature set point (65 °C). Please confirm that 65 mS/cm is the intended conductivity set point, since readings sit near or below it half the time.

### P-A-CAUSTIC: caustic wash flow low

**What it checks:** return flow below **90%** of `Min Return Flow SP` for **5 min** or longer during caustic circulation. The first 5 min are not checked.

**Evidence** (481 cycles; the set point varies by object, with 16 values from 5,500 to 25,000 L/h):

- **Ramp-up.** The set point is reached within 2 min in 90% of runs.
- **Position against set point.** After 5 min, the median valid reading is **113%** of the set point; P25 is 101%, P10 is 44% and P5 is 17%.
- **Below the limit.** 23% of readings are below the set point and 18% are below 90% of it.
- **Invalid readings.** **8%** of readings are negative, which is outside the plausible sensor range, and are not counted.

**Projected:** 14 alerts on 6 objects, which is 2.9% of caustic cycles. All 14 coincide with a caustic Time alert. When the rule fires, the flow is typically at about a quarter of the set point, and the median time below the limit was 6.5 min.

**Sensitivity:**

| Limit | 3 min | 5 min | 10 min |
|---|---:|---:|---:|
| 100% | 34 | 21 | 4 |
| **90%** | 29 | **14** | 3 |
| 80% | 24 | 11 | 2 |

If negative readings counted as zero flow, the rule would give **26** alerts.

**Open questions:**

- Must the minimum return flow be held throughout circulation?
- Are negative readings a meter offset at zero flow (pump off), or a sensor fault?

### P-A-ACID: acid wash flow low

**What it checks:** the same rule as caustic flow, applied to acid circulation.

**Evidence** (61 cycles):

- **Invalid readings.** **29%** of acid circulation flow readings are negative. Each one breaks a low-flow streak.
- **Below the limit.** 30% of the valid readings after 5 min are below 90% of the set point.

**Projected:** **0 alerts**. A 3 min streak would give 3 alerts, and counting negative readings as zero flow would give **10**.

The rule is kept so that the treatment of negative flow can be decided once for all flow rules.

**Open question:** same as for caustic flow. Why is the return flow negative for nearly a third of acid circulation?

### P-A-PRE-RINSE, P-A-MID-RINSE, P-A-FINAL-RINSE: rinse flow low

**What it checks:** return flow below **90%** of `Min Return Flow SP` for **3 min** or longer during rinse circulation. The first **2 min** are not checked.

**Why rinses use shorter timings:** rinse circulation is short. Its median length is 2 min for pre-rinse, 5 min for mid rinse and 4 min for final rinse, and 90% of blocks last under 7–8 min. The 5 + 5 min timing of the chemical washes would almost never apply.

| | Pre-rinse | Mid rinse | Final rinse |
|---|---:|---:|---:|
| Cycles checked | 483 | 61 | 484 |
| Negative (invalid) flow readings | 23% | 19% | 14% |
| Valid readings below 90% of SP (after 2 min) | 36% | 27% | 30% |
| Blocks that never reach the set point | 41 of 488 | 1 of 61 | 8 of 485 |
| **Projected alerts** | **5** (3 objects) | **1** | **6** (4 objects) |
| … also had a Time alert | 5 | no time limit | 5 |
| With a 2 min streak instead of 3 | 16 | 2 | 20 |
| Counting negative readings as zero flow | 22 | 5 | 25 |

**Open questions:**

- Is the flow during rinse circulation a quality parameter, or is the rinse judged on time and final-rinse conductivity only?
- Mid rinse has no time limit in the reference workbook; should it have one?

## 5. Questions that apply to every rule

1. **Severity.** Should proposed T/A/C alerts have a severity level, for example temperature above flow? The monitor has no severity rule today.
2. **Knock-on.**
   - Most proposed alerts happen in cycles where the same activity overran its time.
   - Should the panel present the low T/A/C as the likely cause of the Time alert?
   - If so, what rule defines "knock-on"?
3. **Coverage.**
   - Seven objects have no reference thresholds and are not monitored today.
   - Their set points come from the PLC, so these rules could cover them. Should they?
4. **Alert once per cycle, or once per episode?** Today the proposal alerts once per rule per cycle.
5. **Who signs off.** Name the process and quality owners who approve each rule. Until then, the rules stay in the proposed state.

## 6. How the proposal appears in the CIP Monitor

`CIP_PROPOSED_RULES` sets one of three modes.

**`show` (the default):**

- The debug panel marks each Temperature, Action or Concentration tile with a dashed **Proposed** tag. Its tooltip reads "Proposed — pending Danone sign-off".
- The limit line under the tile reads "Within limit …" or "Below limit …".
- The trend line draws the proposed limit in orange.
- **No alert is raised.** Alert counts stay identical to the notebook: 221.

**`alert`:**

- The rules also raise alerts. These are tagged **Proposed rule**, with a dashed outline in the table column of their parameter.
- They also go to Teams and email notifications when those are configured.
- Use this mode only to trial the rules with Danone. It adds the 47 alerts projected above.

**`off`:** the rules are not loaded.

To change a threshold, edit `rules/proposed_tact_rules.json`. The file only accepts rules with status `proposed`.

When a rule is approved, it should become a normal rule in code and documentation; it should not simply be flagged as approved in this file. The test suite checks that the projected counts quoted in the rule file still match the data.
