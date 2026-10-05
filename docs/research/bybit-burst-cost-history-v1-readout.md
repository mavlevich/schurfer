# Bybit 1-minute burst: executable cost history v1, readout

Protocol: [bybit-burst-cost-history-v1](bybit-burst-cost-history-v1.md) (two design
reviews; the download cap raised from 20 to 40 GiB before any read). Read once on
2026-10-05 at `5f552da` from a clean checkout. Data before 2026-09-29 only.
Descriptive: quoted books are not fills, and the window is the one in which the
trigger was found.

**Evidence** ([bybit-burst-cost-history-v1](evidence/bybit-burst-cost-history-v1/)):

| File                | sha256            | What                                                          |
| ------------------- | ----------------- | ------------------------------------------------------------- |
| `cost-books.json`   | `9a6da537d121...` | 606 of 606 Bybit order-book files (30.4 GB), each with sha256 |
| `cost-funding.json` | `c0cb3efce63a...` | funding settlements of all 282 instruments, status ok         |
| `cost-result.json`  | `b3f1f4a4c722...` | the result                                                    |

`cost-funding.json` (1.25 MB) is above the repository's 1 MB file limit, so it is
committed gzipped as `cost-funding.json.gz` (70 KB, `gzip -n -9`); the sha256 above is
of the uncompressed file. To restore it into a stage directory and check it:

```bash
E=docs/research/evidence/bybit-burst-cost-history-v1
gunzip -c $E/cost-funding.json.gz > STAGE/cost-funding.json
cp $E/cost-funding.json.sha256 STAGE/
test "$(shasum -a 256 STAGE/cost-funding.json | cut -d' ' -f1)" = "$(cat $E/cost-funding.json.sha256)"
```

The 606 order-book files (30.4 GB) are not kept in the repository: each is Bybit's own
public file at the URL in the protocol, and `cost-books.json` holds its sha256, so a
re-download is checked file by file.

**Coverage:** 730 of 732 firings resolved; 2 exit books were stale. That is 282
instruments and 47 days, above every registered minimum.

**Missing from the summary:** the result does not break out fees and funding: the
reader's summary field list missed them (fixed after the read; nothing re-run). Both
are inside the round-trip cost and the net.

## Results

Round-trip cost: the entry and exit depth impact for USD 50, fees on both notionals, and
funding, over the entry notional.

| Entry after B | Half-spread (median) | Entry impact (median / mean) | Round-trip cost (median / mean) | Cost above 41 bps | Net (mean / median) |
| ------------- | -------------------- | ---------------------------- | ------------------------------- | ----------------- | ------------------- |
| 0 s           | 7.7 bps              | 8.8 / 17.1 bps               | 25.9 / 31.3 bps                 | 17%               | +90.6 / +18.8 bps   |
| 2.7 s         | 8.0 bps              | 9.7 / 16.9 bps               | 26.4 / 31.3 bps                 | 19%               | +74.4 / +8.7 bps    |
| 5 s           | 8.2 bps              | 9.5 / 15.7 bps               | 26.6 / 30.1 bps                 | 17%               | +63.7 / -0.6 bps    |
| 10 s          | 8.1 bps              | 9.2 / 16.1 bps               | 26.1 / 30.5 bps                 | 19%               | +45.4 / -20.9 bps   |
| 46 s          | 6.9 bps              | 8.3 / 14.7 bps               | 24.9 / 28.9 bps                 | 15%               | +13.5 / -32.5 bps   |

**Net at the key delays** (descriptive 95% cluster bootstrap):

| Entry | Mean net  | CI by instrument | CI by UTC day   | Share of the net from the top 5 instruments |
| ----- | --------- | ---------------- | --------------- | ------------------------------------------- |
| 2.7 s | +74.4 bps | -7.0 .. +159.6   | -34.8 .. +154.2 | 84%                                         |
| 5 s   | +63.7 bps | -13.5 .. +145.5  | -43.4 .. +140.1 | 94%                                         |

**Gross by executable prices against the trade proxy:**

| Entry | Executable gross | Trade proxy |
| ----- | ---------------- | ----------- |
| 0 s   | +98.7 bps        | +125.0 bps  |
| 5 s   | +71.8 bps        | +95.0 bps   |
| 46 s  | +21.5 bps        | +36.3 bps   |

Walking the book costs about a quarter of the proxy's gross on top of the fees. The
proxy's resolved set may differ slightly.

## Decision: no decision (not parked)

- The median round-trip cost at 5 s is 26.6 bps, under the 41 bps scenario: costs alone
  do not kill the idea. Only about a sixth of the firings cost more than 41 bps.
- Neither interval of the mean net at 5 s lies below zero: the protocol's park rule is
  not met.
- **What the numbers say plainly:**
  - the typical firing nets about zero at a 5 s entry (median -0.6 bps);
  - the positive mean is carried almost entirely by a few instruments (94% of the net
    from five of 282);
  - both intervals still include zero, and speed still matters (+91 at once, +64 at
    5 s, +14 at 46 s).
- **HYP-030 stays alive but weak.** The registered next step is the exit management study
  ([protocol](bybit-burst-exit-management-v1.md)): whether managing the exit keeps the
  few large moves and cuts the rest, on a common denominator, with an exit delay and
  real books.
