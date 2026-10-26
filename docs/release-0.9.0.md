# Climate Pro X 0.9.0

This release makes source freshness, model eligibility and historical estimates
explicit, and adds reproducible analytical evidence and safer history recovery.

## Changes

- Keep complete meter days separate from dates eligible for thermal models.
  Diagnostics explain which source or coverage condition excluded each day.
- Carry final readiness, source dates and model dates through sensors and
  dashboards. Experimental fabric/ventilation estimates inherit HLC problems;
  retained historical values carry their status.
- Export a versioned, anonymized daily evidence bundle in integration
  diagnostics. Independently replay the frozen HLC regression offline with
  `python -m ha_efficiency.evidence downloaded-diagnostics.json`.
- Explain conditional regression uncertainty, show boiler-efficiency
  sensitivity separately, and expose DHW clamp and cooling-night context.
- Validate history document versions before migration, expose structured
  recovery guidance, and pause automatic retries on immutable evidence
  conflicts. Assignment changes request an immediate analytical refresh.
- Add analysis timing/count diagnostics, a portable compact overview card,
  replay tests and CI checks for card readiness states.

## Upgrade and compatibility

The integration manifest is version `0.9.0`; the minimum Home Assistant version
remains `2026.2.3`. Existing entity identities and config-entry schema version 2
are retained. Known legacy history documents migrate without discarding their
records; unknown newer versions are rejected.

Before updating, retain a verified Home Assistant backup covering configuration,
recorder and `.storage`. For rollback, restore matching integration files and
data together using Home Assistant's supported workflow. See
[reliability and rollback](reliability.md).

HACS installs the integration from this release. The optional Lovelace card is
provided separately in `lovelace/climate-x-glance-card.js`; updating an installed
dashboard resource requires the separate setup described in the Lovelace README.
Publishing this release does not update or restart a Home Assistant instance.

## Validation and scope

Local verification passed 377 Python tests on each of Python 3.13 / Home
Assistant 2026.2.3 and Python 3.14 / Home Assistant 2026.9.1, plus four Node card
state tests. Live-core branch coverage was 90.76%; combined coverage was 85.21%.
These checks use fixtures rather than a live household. Card states were tested
programmatically; visual installation acceptance remains separate.

Offline replay verifies the included frozen HLC targets and regression, not
physical calibration, independent DHW retraining, hourly eligibility or
confidence intervals. Dated consumption in diagnostics remains private
household information despite identifier anonymization. See
[analysis evidence and reproducibility](analysis-evidence.md) for details.
