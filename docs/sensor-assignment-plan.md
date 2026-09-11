# Dated sensor assignments for rooms

> Superseded by [Integration-owned sensor and room history](sensor-room-history-plan.md). This earlier proposal resolves history from original source sensors. The revised design records integration-owned source/room streams to preserve captured measurements independently of those sources. Use the revised plan for implementation.

Implementation brief for an agent working in https://github.com/ivmreg/climate-pro-x.

Prepared 2026-09-10 against `main` at `cbd72435a970b508efb3a82df28822928ee2b10d` (manifest version 0.5.1). The local checkout and GitHub main matched when checked. Re-read the current source and repository instructions before implementation if main has advanced.

## Outcome and design choice

A room represents a physical space with a permanent identity. Sensors are replaceable sources assigned to that space for explicit time intervals. Replacing a sensor preserves the room's useful old history and adds only the replacement's history from its effective start. Moving a sensor closes its old-room assignment and opens its new-room assignment. Calculations never reinterpret its entire history as belonging to its latest location.

Considered approaches:

| Approach | Benefit | Limitation |
| --- | --- | --- |
| Follow HA Areas without dated history | Little interaction | Incorrectly applies the latest mapping to old observations. |
| Reset analysis from each change date | Simple and safe | Discards useful heating-season evidence after every replacement. |
| Dated assignments automatically recorded from HA Area changes | Matches Igor's workflow, preserves attributable history and handles repeated moves | Needs event handling, interval validation, correction controls and boundary-aware calculations. |

User-confirmed operating convention: whenever a sensor physically moves, Igor reassigns it to the corresponding Home Assistant Area. Therefore an observed effective Area reassignment is the authoritative move signal. Implement automatic dated assignments from those events, with a manual history/correction workflow as recovery. No confirmation dialog is required for an ordinary unambiguous move observed while the integration is running. Scope the first release to room temperature and heating-power sources; reserve the same primitives for later outdoor, loft, humidity and CO2 source histories. Keep the existing `loft_since` behavior intact. This first release must not claim to solve relocation for those other source roles.

Version 0.7 completes the reserved loft follow-up: loft temperature and humidity now use the same room/source/visit primitives. The thermal calculation still treats a Loft room as the building's loft input, while configuration, Area moves, replacement and history retention follow the common room path. The version-2 config migration converts the old top-level loft fields and reuses the version-0.6 archives; `loft_since` becomes the first dated Loft visit.

Example: Room A used sensor X until 10 September at 14:30, then sensor Y. The room uses X's eligible statistics before the change and Y's afterwards. If X moves to Room B, B receives only X's post-move readings. Replacing hardware while retaining the same entity ID still creates a new assignment boundary.

## Product behavior

The normal move workflow is simply: physically move the sensor, then change its HA Area. The integration records the effective change time, closes its previous room assignment and opens the destination assignment automatically. Existing measurements remain attributed to their former room. Expose the result in a compact assignment status/history view.

Add an options menu with: existing settings, change/replace room sensors, and review assignment history. This supports hardware replacements, backdated corrections and ambiguous cases; it is not a prerequisite for ordinary Area-driven moves. Keep the ordinary settings workflow familiar.

The change workflow asks for:

1. The room and action: replace sources, move a source, or swap room sources.
2. The temperature and heating-power sources after the change. Preserve an unchanged source explicitly; moving temperature does not imply moving the heating source.
3. When the old assignment stopped being valid and when the new one became valid. Default both to now, allow a gap, and allow a past timestamp for a move recorded late. Offer an optional settling delay, default zero; convert it to the new valid-from time and show that time in the summary.
4. A summary of all affected rooms, source changes, effective times and analysis exclusions, followed by Save.

Use Home Assistant local time for input/display and aware UTC for storage. Do not silently interpret ambiguous DST times; require an unambiguous offset/selection, and reject nonexistent local times. First release supports changes effective now or in the past, not scheduling future changes.

A move/swap is one staged transaction covering all affected rooms. A vacated room can receive a replacement in the same transaction or have an explicit data gap. Explain that a gap can reduce whole-home coverage. Cancel or validation failure leaves the complete existing configuration untouched.

History review supports correcting effective times or an incorrectly selected source with a preview of the resulting intervals. This is the recovery route for mistakes; it must use the same validation as a new change. Distinguish correcting an erroneous mapping from recording a real physical change. Ordinary source-selector edits must route through this workflow rather than overwrite old history.

Room display-name changes preserve the room ID and existing sensor unique IDs. Offer an explicit “record replacement/move with unchanged entity ID” path for hardware replacement or a virtual thermostat whose upstream sensor changed. An observed effective HA Area change constitutes a physical move under Igor's convention; an Area rename does not.

## Data contract

Introduce config-entry version 2. Keep the existing room dictionary key as the stable room ID on migration, and generate immutable IDs for newly created rooms. Store the display name separately. Preserve legacy generated names and unique IDs during migration.

Illustrative shape (synthetic entity IDs only):

```json
{
  "rooms": {
    "room_a": {
      "name": "Room A",
      "area_id": null,
      "assignments": [
        {
          "id": "epoch_1",
          "valid_from": null,
          "valid_to": "2026-09-10T13:30:00Z",
          "temperature": {
            "entity_id": "sensor.example_old_temperature",
            "registry_entry_id": null,
            "statistic_id": "sensor.example_old_temperature"
          },
          "heating_power": null
        },
        {
          "id": "epoch_2",
          "valid_from": "2026-09-10T13:30:00Z",
          "valid_to": null,
          "temperature": {
            "entity_id": "sensor.example_new_temperature",
            "registry_entry_id": null,
            "statistic_id": "sensor.example_new_temperature"
          },
          "heating_power": null
        }
      ]
    }
  }
}
```

Contract:

- Intervals are half-open `[valid_from, valid_to)`; null means unbounded on that side. Historical gaps are allowed. Assignments within a room cannot overlap, and at most one is open-ended.
- Each assignment snapshots both roles. A temperature-only replacement copies the heating source into the new assignment. Permit a null temperature source for a vacated room while preserving its independent heating mapping; it produces missing temperature observations. Store a separate `heating_expected` flag: false means deliberately unconfigured; true with no usable source means missing heating coverage. Moving/removing hardware must not silently change expected heating coverage to false. In migration, derive the flag from whether a heating source was configured. A null heating source is never a zero measurement.
- A temperature source cannot cover two different rooms at the same time. Apply the same constraint to room-specific heating sources. Reject newly introduced conflicts; grandfather and surface any duplicate legacy mappings without making an otherwise working installation fail migration.
- Every physical source change has a new epoch ID, even if the entity/statistic ID remains the same. Preserve epoch provenance alongside the composed series.
- `statistic_id` identifies retained recorder history; optional registry identity tracks the live entity. Missing/deleted old entities must not erase a historical assignment. Do not infer that a newly created entity with a reused name is the same physical sensor.
- Resolve entity renames using registry identity only after checking how the supported HA version migrates statistic IDs. Update references to a proven renamed stream without creating a physical-change epoch. If continuity is ambiguous, expose a repair status and require explicit source correction; never silently merge guessed histories.
- Preserve all assignment metadata through normal global-setting edits and reloads. Keep history even when outside the current calculation window; filter queries by interval/window intersection, not by deleting records.

## Calculation rules

Build a pure assignment resolver that returns room temperature series, heating series, heating-expectation metadata, epoch provenance, transition times and quality diagnostics. All room-dependent metrics consume these resolved inputs through one path.

Hourly statistics represent buckets. Include a bucket only if its entire interval is inside one valid assignment. A change at 14:30 excludes the 14:00–15:00 bucket; a change exactly at 14:00 allows the prior bucket in the old assignment and the next bucket in the new assignment. Confirm the recorder API's timestamp/bucket contract in the target HA version. Never split an hourly average by assumed fractions or interpolate a missing interval.

For the first release use a conservative, explicit transition policy:

- Exclude a room's transition local date from room cooling fits. Also reject any candidate fit spanning different source epochs. This catches exact-hour changes that produce no missing bucket and changes in heating-source semantics.
- Exclude a transition date from whole-home temperature/heating-dependent daily modeling and its training sets. Apply this before both heating detection and DHW baseline selection so fallback logic cannot reclassify excluded days. Unrelated electricity/water meter summaries continue normally.
- Route loft analyses through the resolved indoor series and their transition exclusions; existing loft-source handling remains unchanged.
- Keep the fixed room population for whole-home temperature means. Do not drop a vacated or missing room from the denominator to make a result available.
- Where heating was configured, unavailable/invalid readings remain missing. Removing or replacing a heating sensor must not retroactively treat prior missing data as “heating off.” Evaluate whether heating was expected for each interval, rather than consulting only today's configuration. Preserve the legacy unconfigured-heating policy on intervals where heating was never configured.
- Do not invalidate an old heating source's good historical percentage readings because its live entity has disappeared. Validate historical values and source metadata separately from present-state validation. Unsupported historical units/semantics remain a reported quality problem.
- Retain normal coverage and fit-quality rules. Missing retained history produces reduced coverage or insufficient-data status, not fabricated continuity.
- Pool eligible complete observations from different sensors for the same unchanged room, but expose that multiple source epochs contributed. Do not automatically estimate or apply calibration offsets. If replacements differ materially in calibration or placement, users can set the new analysis start through history validity choices, and diagnostics should make the mixed-source evidence visible.

No recorder statistics are rewritten, deleted, copied between IDs, or fabricated. This is an interpretation layer over existing retained data.

## Implementation sequence

### 1. Assignment model and migration

Add `custom_components/thermal_efficiency/assignments.py` for normalization, interval validation and pure history composition. Keep it usable in pure tests without importing Home Assistant; adjust the existing dynamic-load test fixture if relative imports require it.

Update `const.py`, `config_flow.py` and `__init__.py`. Add an idempotent `async_migrate_entry` and version-2 normalization for initial setup/YAML import. Convert each legacy room to one unbounded assignment, preserving its key, sources, global configuration and current outputs. Legacy imports must not later overwrite a version-2 history. Validate before storing and leave original data intact on failure.

Acceptance: an unchanged legacy configuration produces the same inputs, results, entity IDs and unique IDs after migration. No physical history is inferred during migration.

### 2. Recorder reads and calculations

Update `ThermalCoordinator._statistic_ids` to collect every relevant assignment source intersecting the existing statistics lookback (including the longer HLC training window). Deduplicate IDs and retain the batched recorder query. Cache only with configuration revision/window-aware keys if needed; avoid introducing a cache unless measured necessary.

Replace the single-source room loop in `thermal_math.compute_all` with the resolver. Adapt `night_taus`, daily temperature/heating aggregation and all downstream room consumers for epochs, exclusions and per-interval heating expectations. Preserve HLC seasonal anchoring and the fixed complete room population. Ensure all branches that construct DHW/heating training samples respect the exclusions.

Acceptance: historical and new sources are combined only inside their valid periods; exact-hour replacements cannot create a false cooling fit; all unrelated metric regressions remain unchanged for legacy input.

### 3. Options workflow and identity

Implement the change/history workflows in `config_flow.py`; update `strings.json` and available translations. Stage edits in memory and store them once after a complete valid review. Guard against a concurrent options edit by checking the configuration snapshot and asking for a refreshed review rather than overwriting newer data. Use the existing config-entry update/reload mechanism after the single commit.

Update `sensor.py` to use stable room IDs for unique IDs and a separate name for display. Preserve the existing IDs on upgrade. Surface compact attributes such as current source, last change, contributing epoch count, excluded transition days and source-coverage status. Keep detailed source timelines in redacted diagnostics where appropriate rather than unbounded state attributes.

Acceptance: replace, move, swap, repeat move, same-ID replacement, correction, cancellation and unrelated settings edits all retain valid histories and stable room entities.

### 4. Automatic reassignment from HA Areas (required core feature)

Persist room-to-HA-Area associations and each tracked source's last known effective Area. Bind existing rooms to Areas only when the existing source's effective Area and room mapping are unambiguous; otherwise present a one-time association step. Use Area IDs for association, not names. Add registry event listeners using supported HA APIs and proper listener cleanup. Effective entity Area overrides its device Area. Observe relevant area/device/entity updates and check once at startup so offline changes can be noticed. Document that a device Area change has no effective effect on an entity with its own Area override.

For an observed change to another mapped room, close the moving source's old assignment at the event receipt time and open it in the destination room. Persist UTC time and cause (`area_change`) in the history. Apply the earlier bucket/transition exclusions. Only move roles whose effective source Area actually changed; do not infer that the radiator moved with the thermometer. A move that leaves the old room without temperature creates a gap there. If the destination has a temperature source, the incoming source replaces it from that instant, preserving the displaced source's earlier history. A source arriving at a destination where ownership is otherwise ambiguous, including multiple competing arrivals, must produce a pending review instead of selecting arbitrarily.

Track previously configured/retired source identities so a displaced sensor can subsequently be assigned elsewhere, as in a two-step swap. Do not auto-adopt every unrelated temperature sensor placed in an Area: a previously unknown replacement is chosen once through the replacement workflow. That adds it to tracked sources. If source retirement and later re-adoption are ambiguous, require review.

Serialize event processing, coalesce duplicate entity/device notifications without losing the earliest event time, and validate the resulting whole mapping before a single configuration update. A short burst of moves may be committed together; distinct event times must remain distinct in the timeline. Handle replay/idempotency and prevent config-update/reload loops. Preserve the existing reload behavior initially if viable, but test that rapid changes around reload cannot be lost; use a long-lived entry-scoped event processor or another supported lifecycle design if necessary. This must be tested, not left to timing assumptions. Manual options saves must check for concurrently applied automatic moves.

If a device moves to an unmapped Area or loses its Area, close the old assignment at the observed time and report the source as unassigned. Do not automatically create a new modeled room or change the whole-home population. Let the user associate/create the destination room and then resolve the pending assignment using the recorded event time.

On startup, a mismatch with the persisted Area proves that a change occurred, but not when. Record the last successfully verified Area timestamp while running (at bounded intervals, avoiding frequent storage writes). Quarantine uncertain source observations after that timestamp and present one deduplicated issue asking for the actual move time. Preserve earlier evidence. Resume the new-room interval from the supplied time; do not use startup time as a guessed historical move date. A first-upgrade mismatch with no trustworthy prior timestamp needs explicit history review. Area rename changes presentation only; deleted Area retains the room/history and requires association repair. Use UI issues, not external messages or push notifications.

Virtual-thermostat source caveat: the existing room picker often selects an EMA sensor on a virtual thermostat device. Moving a physical upstream thermometer may not move that EMA entity's Area and may not change the virtual thermostat's inputs. Inspect this source relationship explicitly. Default automated tracking follows the actual configured statistic source; never assume HA Area reassignment reconfigures a thermostat. If direct physical-sensor tracking is desired, allow selecting that sensor as the room's source. Alternatively, store an explicit user-selected physical Area anchor for a virtual source only if the virtual series is guaranteed to follow that sensor. If the guarantee cannot be established, surface the limitation and require a source selection/correction. Do not modify thermostat controls as part of analytics reassignment.

Acceptance: Igor moves a tracked sensor between mapped HA Areas and does nothing in climate-pro-x; its new measurements automatically follow it, its old history remains in the old room, and generated room entities keep their identities. Back-to-back device/entity events and two-step swaps survive reloads without duplicated/lost epochs. Offline moves and unsupported virtual-source relationships are explicitly unresolved rather than guessed.

### 5. Documentation, verification and delivery

Document the change workflow, gaps, retained-history limits, calibration caveat, same-ID changes, Area-versus-physical-move distinction and first-release scope in `README.md`. Update example configuration only as necessary for the supported import contract. Do not include household entities, locations, credentials or live data in examples/tests.

The offline CLI uses a separate configuration and some separate mathematics. Preserve its existing behavior and tests; do not imply it supports these new histories until a separate CLI adapter is implemented. Bump integration release metadata according to repository conventions and describe the versioned migration.

## Required tests

Use deterministic synthetic data with markedly different temperatures between rooms so accidental historical reassignment cannot pass unnoticed.

| Scenario | Required assertion |
| --- | --- |
| Legacy migration and repeat migration | Equivalent results and IDs; no data lost; idempotent. |
| Replacement with a sensor that already has history | Ignore replacement's pre-assignment readings; preserve old source's earlier contribution. |
| Move A to B; move back later | Each sample belongs only to its room at that time; histories survive repeated moves. |
| Atomic swap | Both rooms change together; cancelled/failed flow changes neither. |
| Same entity ID, new hardware | Epoch boundary remains and blocks transition fits. |
| Change at hour boundary and mid-hour | Correct bucket inclusion; no mixed hourly means or false cooling fit. |
| DST, midnight, gap and settling interval | UTC ordering and local exclusions correct; ambiguous times rejected or disambiguated. |
| Temperature changes but heating does not | Heating remains mapped to the correct room; unchanged history retained. |
| Heating added, removed, replaced or unavailable | Expected coverage is evaluated per interval; missing never becomes zero; no unsafe historical fallback. |
| Retired entity absent but statistics retained | Historical contribution survives without live-state validation rejecting it. |
| Old statistics missing | Explain reduced coverage; never reuse the new source's older unrelated history. |
| Room/Area rename and actual Area reassignment | Renames preserve attribution; observed effective reassignment automatically starts the correct new epoch with stable room entities. |
| Proven entity rename and ambiguous ID reuse | Correct continuity for rename; no guessed continuity on reuse. |
| Overlap, duplicate assignment, invalid timestamp | Reject new conflicts before any config write; report grandfathered legacy conflicts. |
| Global settings edit after several moves | Full assignment history preserved. |
| Source discontinuity | Transition day excluded from cooling/HLC/DHW training; unaffected meter summaries retained. |
| Full synthetic pipeline | HLC seasonal anchoring, DHW attribution, loft and existing coverage gates still work. |
| Concurrent options flows | Second stale save cannot discard first flow's changes. |
| Automatic move during manual options flow | Stale manual save cannot erase the automatic event. |
| Repeated registry events and a two-step swap | Idempotent serialized changes, preserved times, and no source lost across reloads. |
| Destination already occupied | Incoming tracked source replaces its role from the event time; displaced history remains available. |
| Unmapped/no Area and offline change | Old assignment closes when known; no invented destination room/date; uncertain history quarantined pending repair. |
| Device Area overridden on entity | Only an effective Area change triggers reassignment. |
| Physical sensor behind virtual thermostat | No unproven reassignment of an EMA series or thermostat input; explicit source/anchor behavior verified. |
| Real HA lifecycle fixtures | Migration, saved flow/reload, registry listener cleanup and issue dismissal work together. |

Add focused pure tests (for example `tests/test_sensor_assignments.py`) and HA fixture tests alongside `tests/test_homeassistant_integration.py`. Run the full current CI commands in a Python 3.13 environment with the repository's dev dependencies:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest
python -m coverage report --include='custom_components/thermal_efficiency/thermal_math.py' --fail-under=90
```

Existing overall branch coverage threshold is 80%. Add meaningful branch coverage for the new resolver, migration and flow logic. Ensure HA fixture tests actually execute: the current file uses `importorskip`, so a run without HA dependencies is not sufficient evidence. Do not lower existing thresholds to make the change pass.

## Agent handoff and release boundary

Implement in the GitHub repository, checking the current branch and instructions first. All five steps, including automatic HA Area reassignment, are required for the requested core feature. Use official HA documentation/source to verify recorder bucket semantics, registry identity/rename behavior, config migration and Repairs APIs for the supported version before coding those boundaries.

Deliver a reviewable diff/PR with the user-visible behavior, schema migration, exact test results and known limitations. No live Home Assistant rollout is part of this planning request. Before any eventual installation, preserve the prior integration version and a scoped config-entry backup through supported means. Rolling back to old code alone is insufficient after schema migration: restore its matching configuration snapshot as well. The raw recorder history remains untouched throughout.

Definition of done for the core: a tracked sensor can be moved twice or swapped with another room by changing its effective HA Area, with no additional climate-pro-x interaction for ordinary observed moves; replacements and corrections are supported through the integration UI; each room keeps its entity identity and correctly attributed historical evidence; transition artifacts cannot enter the model; migration and existing pipeline tests pass; documentation accurately states what is supported.
