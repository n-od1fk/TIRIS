# ADR 0001: Use ReadPrism as a design reference without copying its implementation

- Status: Accepted
- Date: 2026-09-22

## Context

TIRIS is licensed under the MIT License. ReadPrism is licensed under the GNU
Affero General Public License version 3 (AGPL-3.0). ReadPrism addresses several
related problems, including feed ingestion, reading telemetry, temporal
interests, explainable ranking, and adjacent-interest discovery.

TIRIS has requirements that differ from ReadPrism. In particular, TIRIS needs
content versioning, acquisition-level provenance, ingestion-run records,
cross-source relationships, append-only behavior events, fixed delivery rules,
and time-aware recommendation evaluation.

## Decision

TIRIS will remain MIT licensed. ReadPrism may be studied as a product and
architecture reference, but its source code, tests, comments, migrations,
templates, and assets will not be copied into TIRIS.

Relevant concepts may be independently implemented when they fit TIRIS. Each
implementation must use TIRIS-specific data contracts and tests, and its design
must be justified from TIRIS requirements rather than inherited unchanged.

Before introducing a feature inspired by ReadPrism, the implementation decision
must state:

1. the TIRIS problem being solved;
2. the part of the ReadPrism design being considered;
3. why direct reuse is unsuitable;
4. the TIRIS-specific changes and acceptance criteria.

Any future proposal to copy or modify ReadPrism implementation code requires a
separate license review and an explicit license decision before code changes.

## Concepts worth evaluating independently

- visible and active reading time instead of wall-clock dwell time;
- source fetch health that is visible to the user;
- explicit signal breakdowns saved with recommendation exposures;
- long- and short-term interest separation with time decay;
- interest-adjacent exploration instead of unrelated random content;
- leakage-aware learning and time-ordered evaluation.

These are candidate hypotheses, not already validated TIRIS features.

## Consequences

- TIRIS retains the flexibility of the MIT License.
- The project will not inherit ReadPrism's AGPL obligations through copied code.
- Similar features may take longer to implement, but their behavior and trade-offs
  will be explainable and testable by the TIRIS maintainer.
- ReadPrism remains an attributed design reference, not a code dependency.
