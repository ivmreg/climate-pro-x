# Climate Pro X 0.10.0

Short interruptions no longer automatically discard a full HLC day when
temperature and heating evidence meet bounded, auditable conditions.

- Recorded partial-hour temperature means may be used across one internally
  bracketed gap of at most 120 seconds, limited to two hours per room per day.
- Temperature gaps up to three hours may be interpolated between valid covered
  hours, with a maximum of two interpolated hours per room per local day.
  Assignment boundaries, quarantines, missing endpoints, contaminated values
  and longer gaps remain excluded.
- Heating-off classification still requires complete expected coverage. When
  no more than two heating hours are missing, a day can be used as heating-on
  only if the observed nonnegative demand proves the full-day mean exceeds the
  heating-off threshold. No missing heating demand is assumed to be zero for
  hot-water baseline training or room-cooling fits.
- Gas meter day completeness remains strict. Room-cooling and loft calculations
  continue to use the original strict hourly streams.
- Version-2 analysis evidence reports partial and interpolated temperature
  hours, heating coverage and a strict-policy HLC comparison on the same
  snapshot. The usual regression interval does not include gap-estimation
  uncertainty. Version-1 evidence remains replayable.

The integration requires Home Assistant 2026.2.3 or later. Its configuration
and existing entity identities are retained. A Home Assistant restart is needed
after downloading the release to load the updated Python code. Keep a verified
backup of Home Assistant settings, integration storage and recorder data before
upgrading; see [reliability and rollback](reliability.md).

See [analysis evidence](analysis-evidence.md) for details and interpretation.
