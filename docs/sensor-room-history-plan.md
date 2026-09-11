# Integration-owned sensor and room history

Agent implementation plan for https://github.com/ivmreg/climate-pro-x.

Prepared 2026-09-10 after inspecting `main` at `cbd72435a970b508efb3a82df28822928ee2b10d`, integration version 0.5.1. Recheck current repository instructions and source before implementation. This supersedes `sensor-assignment-plan.md` as the implementation brief.

## Intended behavior

Igor physically moves a sensor and changes its Home Assistant Area. Climate-pro-x automatically records subsequent measurements against the new room, keeps previously captured measurements against the old room, and continues calculating results grouped by stable room identity. Deleting or replacing the original source must not remove history already recorded by climate-pro-x.

Use three layers:

```text
Physical or configured virtual source
    -> climate-pro-x measurement entity for (source identity, room identity, role)
        -> room series assembled from valid assignments
            -> existing room cooling and whole-home calculations
```

The middle layer is a real recorder-backed measurement stream. Merely making an alias entity and continuing to query the original sensor's statistics would not provide the requested independence.

Example: source X starts in Room A, moves to Room B, then returns to A. The X/A entity records during the first and third visits and is unavailable during the second. X/B records only during the second visit. Each visit is separately dated, even though the X/A entity is reused. Source Y replacing X in A creates a Y/A entity; the room model combines eligible historical X/A and Y/A observations by time. It never averages all historical source/room entities indiscriminately.

## Identity and persistence

Persist a versioned model containing:

- Rooms: immutable internal room ID, display name, associated HA Area ID. Preserve existing room keys and generated analysis entity unique IDs on migration.
- Sources: immutable internal source ID, current entity-registry identity/reference, role, unit metadata and last verified effective Area. Retain tombstones for deleted sources. Never use a mutable friendly name as identity. A newly registered source reusing an old entity ID is a new source. Same-registry-ID hardware replacement requires an explicit replacement action if no reliable identity change is exposed.
- Streams: immutable ID for `(source_id, room_id, role)` and its independently owned measurement entity identity. Persist its resolved recorder statistic ID so naming collisions or user renames are handled. Roles initially include room temperature and room heating percentage.
- Visits: immutable visit ID, stream ID, UTC start/end, observed event time, cause, uncertainty/validity flags and any settling gap.
- Room selections: dated choice of contributing stream per role, including explicit gaps and whether heating coverage was expected. Keep this distinct from which streams merely exist in a room.
- Availability/quality intervals sufficient to reject partial coverage after raw recorder history expires. Define and test retention to cover the longest supported model and training lookback; avoid storing every raw sample in config-entry data.

Keep user configuration in config entries and use supported versioned HA integration storage for runtime history metadata. Do not serialize high-frequency measurements into config entries or trigger a config reload per sample. Establish one ordered event processor, a durable assignment journal, and an idempotent reconciliation process because HA storage, entity state writes and recorder commits are not one atomic transaction. If restart exposes an incomplete transition, quarantine its uncertain interval and reconcile; never assume partial writes were atomic.

Generated stream entities belong to climate-pro-x room devices, not to the source integration's device or config entry. Deleting a source integration must not cascade removal of these entities. Recreate retained inactive streams as unavailable after restart. Do not automatically delete entities, metadata or recorded history when a source moves or disappears.

An Area rename changes presentation only. Removing an Area retains the internal room and its history; it requires reassociation rather than deletion. Changing a room device's display Area must not redefine historic room identity.

## Recording pipeline

Add event-driven measurement entities with appropriate units, device class and measurement state class. Temperature is normalized consistently; heating is finite 0–100 percent and must not be mistaken for watts. HA supports long-term statistics for sensors with suitable properties; recorder inclusion must also permit them. Reference: https://developers.home-assistant.io/docs/core/entity/sensor/ and https://www.home-assistant.io/integrations/recorder/.

Mirror valid source observations promptly through a dedicated source manager. The current six-hour analytics coordinator is not a suitable sampling mechanism. Source-state/registry listeners must have supported lifecycle cleanup, serialized ordering and no recursive subscription to climate-pro-x's own output entities. Verify the target HA APIs for equal-value reports and freshness; a cached old state is not proof of a measurement in the new location.

When a source moves, stop the old stream and mark it unavailable; do not leave a frozen last value recording indefinitely. Start the destination stream only after an eligible source observation at or after the move (and any configured settling time). A destination stream must not inherit a cached pre-move reading. Validate source timestamps/provenance where supplied and use a documented freshness policy. Unavailable/unknown/invalid source values produce missing data, never zero. At startup, do not republish restored numeric states as fresh source measurements; verify current source and Area first.

Persist quality intervals so recorder hourly means based on a short valid fragment cannot be mistaken for complete-hour evidence. The analytics resolver must use this metadata, not just the presence of an hourly mean. Define unavailable, stale, disabled, offline and transition handling explicitly, including conservative handling when an integration restart leaves coverage uncertain.

Inactive entities remain enabled and unavailable so their histories remain addressable. Avoid state-attribute growth: full visits and quality history belong in integration storage/diagnostics; entity attributes should expose only current source, active status and recent change summary.

## Automatic moves and replacements

Resolve effective source Area from the entity override first, otherwise its device. An effective Area change is an authoritative physical-move signal under Igor's convention. A device change masked by an entity override does not move the stream.

For tracked sources moving between mapped rooms, automatically close the previous visit and open the new one at the observed UTC event time. Update dated room selections. No additional confirmation is required. Temperature and heating roles move independently: moving a thermometer does not move a radiator's source.

Maintain one selected stream per room/role in this release. If a tracked source arrives in a room with an existing selected stream, select the incoming stream from the event time and close the previous selection; retain the previous stream and its historical evidence. It can remain observable in that room, but is no longer a model input. Expose this replacement in status. Multiple competing arrivals in one unresolved transaction require review rather than arbitrary selection. Do not average multiple temperature sensors by default.

Support two-step swaps and later returns by retaining source identities and reusing source/room streams. Serialize/coalesce duplicate entity/device events while preserving distinct effective move times. A source leaving a room without a replacement creates a coverage gap; do not silently remove that room from whole-home analysis. Preserve heating expectations through gaps.

Moving to no Area or an unmapped Area stops attribution to the old room. Record a pending destination and offer room association. Do not automatically grow the modeled room population. Unknown sensors are not adopted merely because they appear in an Area: a replacement picker registers the new source once and begins its visit. Same-ID hardware replacement creates a new source generation or visit boundary through that workflow.

Provide a simple source registration/replacement flow and history correction flow. Inputs are local time with explicit DST disambiguation; persist aware UTC timestamps. Manual edits stage a full preview and validate intervals before save. Protect against concurrent automatic moves with a configuration/history revision check. Permit backdated corrections and explicit gaps; defer scheduled future moves.

On startup, compare persisted and current Areas. If a move happened while disconnected, its physical time is unknown: quarantine ambiguous history after the last verified mapping and ask for the move time. The generated destination stream begins only with new trustworthy observations. Do not invent observations for downtime. Provide a narrowly scoped way to exclude/reassign erroneous visits in the analytical resolver without rewriting recorder history. Existing displayed stream history is an as-recorded audit; corrected room-model attribution may differ and must be labeled. A correction cannot recover samples never recorded.

Virtual-source limitation: current setup often selects a virtual thermostat's EMA sensor. Moving an upstream physical thermometer may leave that virtual entity and its inputs unchanged. Track the actual configured source. For physical-sensor Area automation, allow using the physical sensor directly, or an explicit physical anchor only where its virtual series is proven to follow that sensor. Never infer or change thermostat inputs/control behavior from Area metadata alone.

## Room grouping and mathematics

Keep source/room stream entities as canonical recorded inputs. Group them under stable room devices for inspection and compose room series in the resolver. Existing room analysis entities remain stable. A second set of continuously recorded room-average entities is unnecessary for the first release; it would duplicate storage and still require provenance rules.

The resolver queries owned stream statistics, filters by visits, room selections and quality intervals, and returns one temperature/heating observation per room/time with source provenance. Use all historically selected streams intersecting the existing model/training lookback, not just current streams. Retained owned histories remain readable when sources no longer exist.

Use half-open UTC intervals. Validate the target recorder bucket contract. Only accept complete hourly buckets inside a valid visit/selection/availability interval. Reject the move-containing bucket for a mid-hour move. Even at an exact hour boundary, never fit one cooling curve across different source visits. Conservatively exclude the transition local date from room cooling, whole-home temperature/heating modeling and DHW training/fallback classification. Route loft's indoor inputs through the same corrected room series. Unrelated meter usage summaries remain unchanged.

Preserve fixed room population, coverage gates, HLC seasonal anchoring and normal fit validation. Evaluate heating expectation by historical interval; missing expected heating must never become evidence of heating off. Do not reject an owned historical percentage stream because its original source is currently absent. Pool eligible complete nights for an unchanged room across visits, report contributing sources, and do not automatically calibrate sensor offsets.

## Mandatory preservation of existing history on upgrade

User requirement: historical data preservation is a release-blocking part of the upgrade, not an optional later import. Preserve all retained history for the integration's configured source inputs, including records older than the current analysis window. Do not truncate preservation to `max_window_days` or the HLC training lookback. Calculation windows restrict use, not retention.

Upgrade the existing version-1 configuration without changing current analysis entity identities or global settings. Map legacy sources/rooms to stable IDs. Keep an intact configuration snapshot and a migration manifest. Preserve original recorder data in place throughout: copy, never move, rename, overwrite or delete source history as part of migration. Existing derived analysis entities retain their recorder history as well as their unique IDs; do not recreate them under fresh IDs.

### Required migration pipeline

1. **Inventory and snapshot:** enumerate every configured input statistic ID, including retired entities whose statistics remain. Record available date bounds, units, statistical semantics, metadata and old source-to-room mappings. Capture all retained long-term statistics and any still-retained short-term statistics/raw source state history through supported recorder APIs. Archive raw and short-term records first where they are at risk of routine purge. Do not expand this into collecting unrelated household entities. Historical metadata can be private; archives stay local and must never be committed or logged in full.
2. **Durable preservation:** write the retrieved data into a versioned, integration-owned migration archive with immutable, losslessly serialized chunks and a manifest. Use supported HA integration storage/file lifecycle, atomic chunk writes, bounded memory and resumable checkpoints. Preserve timestamps, values, nulls, units, statistical fields and provenance at the resolution actually retained. Do not filter invalid/outlier/uncertain observations out of this archive; analytical exclusions are separate. Include the archive in documented backup/recovery scope and never automatically age it out with model windows or source removal.
3. **Independent historical statistics:** import the archived long-term observations using the supported external-statistics API for the target HA version into a stable climate-pro-x namespace. Give each imported source generation its own owned statistic identity; attach historical room assignments separately. Keep native live measurement statistics and imported external histories distinct in storage and join them in the resolver. Verify the installed API and metadata requirements rather than assuming a particular helper signature exists. Never insert old observations as present sensor states or edit recorder database tables directly. Retain the lossless archive even when some archived fields cannot be represented by the external-statistics schema.
4. **Verify before switching:** wait for recorder import processing and re-read the destination. Compare per-source counts, complete ordered timestamps, available fields and normalized values against the source archive; record chunk digests and explicit floating-point tolerances if unit conversion is unavoidable. Matching only first/last timestamps or aggregate sums is insufficient. Check that model results using the imported inputs match the old path on the same fixed evaluation date, mappings and windows. A gap, conversion error or unsupported source remains visibly incomplete; never silently omit it and call migration successful.
5. **Cutover:** start live owned capture promptly while the historical job runs. Choose and persist an aligned hourly boundary `T` at which live capture is known operational. The resolver uses migrated source history for buckets before `T` and owned live history for buckets from `T` onward. Reconcile late-finalized recorder buckets near `T` before final verification. Keep every archived record, even overlapping/excluded transition data; the resolver deduplicates by explicit provenance/time rules. Real moves during migration still use visit exclusions. There must be no migration-induced missing or double-counted complete bucket.
6. **Commit completion:** mark history migration `complete` only after every scoped source's archive, import and parity checks succeed. Separate fast config-schema migration from this resumable data-migration state machine; do not block HA's event loop or startup for an unbounded import. While copying/verifying, preserve the old calculation read path where still available and keep new capture running, with migration status visible. An interrupted, disk-full, unavailable-recorder or failed verification run remains resumable and never discards originals, restarts from a blank history, or declares success. Completion is a durable state transition recorded only after all required writes are verified.

After completion, historical analytical reads must use the integration-owned imports/archive and live owned streams. Original-source fallback is allowed only during an explicitly incomplete migration. Data preservation must not depend on the original source's continued existence after successful migration.

Raw/short-term history is preserved as an inspectable archive at its original retained resolution; do not claim that it has been recreated in HA's normal entity History UI. Imported long-term statistics must remain queryable and usable by the room model. Document where users can inspect/export the historical archive and view the combined analytical history.

### Attribution, missing data and recovery

Preserve existing legacy room assignments as the initial historical interpretation, with provenance marked `legacy_mapping_unverified`. Do not relabel all old history using today's HA Area or infer unknown past move dates. Store source history independently so room attribution can later be corrected without losing or recopying observations. Preserve ambiguous history in the archive even if the room resolver cannot safely use it yet. A data-preservation-complete status and an attribution-needs-review status are separate facts.

History already purged or absent before the upgrade cannot be recovered by migration. The inventory must distinguish preexisting absence from migration failure and report the exact preserved time bounds/resolutions. Do not fabricate raw readings from hourly aggregates. If history disappears during snapshotting, do not call the preservation check complete; report the affected range and recover from an available authorized backup where possible.

Retry imports by stable source/timestamp identities with checkpoints and integrity checks. A crash after import but before acknowledgement must not create duplicates on restart. Never overwrite a previously verified archive chunk merely because the source later changed; record a new snapshot revision and reconcile explicitly. If deleting the original source or its original statistics in a test breaks preserved history after completion, the feature is not ready to release.

Recording protection also depends on HA recorder configuration and storage retention. Deleting/purging climate-pro-x's own data or losing the database is outside source-deletion protection. Do not alter recorder filters automatically; detect/report blocked recording through supported checks and document backup requirements.

## Implementation work packages

1. **Model and mandatory data migration:** add `assignments.py` (pure interval/selection logic), `store.py` (versioned runtime metadata), and `history_migration.py` (archive/import/verification state machine); update `const.py`, `__init__.py` and `config_flow.py`. Preserve old IDs and config compatibility. Implement the complete preservation pipeline above; a schema-only upgrade does not meet the requirement.
2. **Owned measurement capture:** add `source_manager.py` and measurement entity classes in a separate module used by `sensor.py`. Decouple capture startup/lifetime from the heavy analytical first refresh so waiting for enough historical data never prevents recording. Register/recreate stream entities dynamically, with room device ownership and stable identities.
3. **Automatic assignment engine:** subscribe to supported registry/source events; handle source moves, returns, replacement, retirement, stale events, offline reconciliation and journal recovery. Source state changes must not reload the integration. Implement dynamic stream addition and properly serialized runtime mapping updates.
4. **Analytics adapter:** update `coordinator.py` statistic collection and `thermal_math.py` room input construction to use owned live streams plus independently imported legacy history. Use original-source reads only while migration is explicitly incomplete. Centralize provenance/quality filtering for all downstream room consumers. Preserve independent pure math tests, adjusting their dynamic loader if imports change.
5. **UI and diagnostics:** add source replacement and history correction options, Area association, pending issue handling, and compact room/stream status. Update strings/translations. Prevent stale manual saves from overwriting events. Store no private household examples in tests or docs.
6. **Verification and documentation:** add tests below, document capture cutover and deletion guarantees precisely, update version metadata according to repository convention, and provide a reviewable PR/diff. Preserve the separate offline CLI behavior; do not claim it supports these new histories unless separately adapted.

## Acceptance tests

Use synthetic sources with sharply different room temperatures and real HA recorder fixtures where recording/statistics behavior is under test.

- **Actual independent recording:** source changes appear on owned entities promptly, independent of six-hour analytics refresh. Recorder produces the correct owned statistics. Test a constant-value source and unavailable transitions, not only changing values.
- **Delete original source:** after recording multiple complete buckets, remove the source entity/device/config entry in a fixture. Owned entities and statistics survive; the room model still uses old observations without querying the removed source for that period. Restart and repeat the assertion.
- **Move, return and swap:** X/A -> X/B -> X/A reuses streams but creates distinct visits. Two-step swaps preserve all earlier histories and select only the correct current stream.
- **Replacement:** Y/A replaces X/A without replacing the room entity. Y's unrelated preexisting original history never enters A's post-cutover owned history. Same-name ID reuse never conflates physical identities.
- **Freshness and ordering:** destination never receives the cached pre-move reading. Duplicate/late state and registry events, unchanged-value reports, moves during initialization and partial journal commits are handled deterministically.
- **Grouping:** two streams in one room cannot accidentally double-weight that room. Incoming selected source replacement and ambiguous arrivals follow documented rules.
- **Boundary/quality:** exact-hour and mid-hour moves, DST, settling gaps, unavailable/stale sources and partial statistics cannot create false cooling fits or misleading DHW classifications.
- **Mandatory old-history preservation:** populate source history spanning multiple years, longer than the configured model window, including all retained raw/short-term/hourly records, nulls and metadata. Upgrade and compare the entire archive/import against the original fixtures. No observations outside the model window may be dropped. Assert original source records and existing derived analysis entity history are unchanged.
- **Delete original after migration:** after migration reports complete, remove the original source and purge only its original statistics in an isolated HA test fixture. Restart HA. Assert all migrated history remains accessible, correct and usable by the room model, with zero reads of the original source IDs. This is required independently of the live-capture deletion test.
- **Migration/cutover:** old-path and imported-path results match for a fixed evaluation time; IDs remain stable; aligned cutover, a move during copying, and late recorder finalization introduce no duplicate or missing complete buckets.
- **Migration recovery:** restart after archive write, during import, after recorder import but before checkpoint acknowledgement, and during verification. Include disk-full/recorder-unavailable/partial-import failures. Each retry resumes safely, preserves originals and creates no duplicate records. Incomplete states cannot report success.
- **Preservation verification:** a missing interior bucket, altered value, unit mismatch or incomplete field copy must fail verification even when counts or time bounds happen to match. Preexisting source gaps remain documented gaps, not invented readings.
- **Historical attribution:** mismatched current Areas cannot rewrite legacy archived data; unknown old moves remain correctable metadata while the preserved source history remains intact.
- **Lifecycle and ownership:** source removal cannot cascade-delete generated room devices/entities; inactive streams are recreated unavailable; listeners are cleaned up on unload; no feedback loops or per-sample reloads.
- **Offline/correction:** uncertain periods excluded until resolved; corrections affect model attribution without erasing recorded audit history; no fabricated downtime samples.
- **Recorder exclusions:** disabled/excluded owned recording gives actionable status, not a claim of protected history. Do not modify recorder settings in the test target outside fixtures.
- **Coverage/regression:** independent heating expectations, fixed room population, HLC seasonal anchoring, loft inputs, existing full synthetic pipeline and unrelated meter metrics remain valid.

Use repository CI running across both Python 3.13 (HA 2026.2.3) and Python 3.14 (HA 2026.9.1) with `requirements-dev.txt`:

```sh
python -m pip install -r requirements-dev.txt
python -m pip install homeassistant==2026.2.3  # or 2026.9.1 on Python 3.14
python -m pytest
python -m coverage report --include='custom_components/thermal_efficiency/thermal_math.py' --fail-under=90
```

Retain the existing overall 80% coverage requirement. Add meaningful coverage for the new source manager, interval resolver and storage lifecycle. Ensure HA tests actually run; the existing fixture suite uses `importorskip`, so passing without HA dependencies is insufficient.

## Delivery and verification boundary

Verify supported HA registry identity, source event timestamps, recorder bucket semantics, entity removal behavior and native/external statistics APIs using official documentation/source before implementing those boundaries. Useful references: https://developers.home-assistant.io/docs/core/entity/sensor/, https://data.home-assistant.io/docs/statistics/, https://www.home-assistant.io/integrations/recorder/.

Deliver source changes and test evidence for review. No live HA installation, restart, recorder change or deployment is authorized by this planning task. An eventual release must back up prior integration configuration, the new runtime assignment metadata, the migration archive/manifest and relevant recorder data. Rollback must restore matching code/schema rather than merely downgrading files, while retaining new archives and owned histories for recovery. Raw source and owned recorder data must not be deleted by migration or rollback.

Done means: physical moves followed by HA Area reassignment route newly observed values into the correct owned stream automatically; all scoped retained pre-upgrade data is independently archived and its long-term statistics imported and verified; both migrated old history and new captured history survive deletion of the original source/statistics; repeated visits remain distinct; room results and identities persist; recovery and meaningful recorder/lifecycle tests pass. Mandatory historical preservation cannot be deferred to a follow-up release.
