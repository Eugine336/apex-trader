# Apex historical concept hindcast

The hindcast runner replays the registered concept modules and native Brain over
historical OHLC without connecting to a broker or submitting orders.

## Dataset manifest

Place the manifest beside the referenced CSV files. Timestamps must be UTC and
must be explicitly identified as either bar-open or bar-close times. Use
`bar_close` for D1 candles; daily session lengths can vary across DST/session
boundaries, so a fixed 86,400-second close-time derivation is not accepted.

```json
{
  "schema_version": 1,
  "symbol": "EURUSD",
  "source_reference": "Broker export identifier or vendor dataset reference",
  "attestation_basis": "How the source timestamp and export integrity were checked",
  "source_timestamp_verified": true,
  "timestamp_semantics": "bar_open",
  "timestamp_timezone": "UTC",
  "timeframes": {
    "M1": {
      "file": "EURUSD_M1.csv",
      "sha256": "64-character SHA-256 digest"
    },
    "M5": {
      "file": "EURUSD_M5.csv",
      "sha256": "64-character SHA-256 digest"
    },
    "H1": {
      "file": "EURUSD_H1.csv",
      "sha256": "64-character SHA-256 digest"
    }
  }
}
```

CSV headers accept `time,open,high,low,close` and common aliases. Volume is
optional. The loader validates positive finite OHLC, OHLC consistency, strictly
increasing timestamps and duplicate bars. Each file is checked against its
manifest SHA-256 before use.

The manifest attestation is a caller declaration; the runner does not
independently authenticate a broker or vendor. The digest makes the exact
declared dataset reproducible and detects changes after the manifest is made.
Do not set `source_timestamp_verified=true` unless the source and timestamp
semantics were actually verified.

For PowerShell, calculate a file digest with:

```powershell
(Get-FileHash .\EURUSD_M1.csv -Algorithm SHA256).Hash.ToLower()
```

## Run

```powershell
python scripts/run_concept_hindcast.py .\hindcast-manifest.json --base-timeframe M5 --horizon-seconds 86400 --output .\hindcast-report.json
```

The outcome horizon is mandatory and explicit. `--decision-stride 1` evaluates
every completed base-timeframe bar. Increase it only when the reduced decision
coverage is acceptable and report the stride with results.

The runner:
- exposes only bars completed at or before each decision cutoff;
- runs the registered concept readers and the native Brain at that cutoff;
- labels activated directional concept implications using their own objective
  and invalidation prices, starting strictly after the decision cutoff;
- treats a same-bar objective/invalidation double touch as ambiguous;
- scores Brain confidence only when an actionable market-order opportunity has
  explicit stop/target geometry, its geometry evidence IDs resolve to evidence
  visible at that cutoff, and one level resolves first;
- selects non-overlapping Brain outcome windows by decision time, independent
  of their eventual labels;
- records per-concept applicability, source-time verification, missing-data
  unknowns and reader faults, alongside unresolved and ambiguous outcomes,
  rather than converting them into losses or wins.

Concept strength is retained as uncalibrated reasoning strength, never treated as
a probability. Per-concept win rates are descriptive historical rates, not
current-regime predictive probabilities. OHLC level-order labels do not include
spread, slippage, financing or execution latency; they are not net-P&L claims.

## Calibration persistence

The default run is report-only. To explicitly seed the persistent
`CalibrationTracker` from resolved Brain confidence/outcome pairs:

```powershell
python scripts/run_concept_hindcast.py .\hindcast-manifest.json --base-timeframe M5 --horizon-seconds 86400 --seed-calibration
```

The source key includes the dataset digest, base timeframe, horizon, stride and
hindcast protocol version. Re-running the identical replay does not append the
same calibration samples again. No calibration is written if there are no
resolved Brain samples.

## System 4 boundary

The hindcast uses OHLC. It does not manufacture bid/ask quote evidence from
candle closes. Therefore it does not issue producer-owned System 4 expected
return artifacts, whose current eligibility protocol requires verified
decision-time and horizon quote events. Those artifacts remain data-gated until
such quote history is supplied.

No historical data is bundled in the repository. A real calibration result
cannot be claimed until an owner-supplied, source-attested dataset is replayed.
