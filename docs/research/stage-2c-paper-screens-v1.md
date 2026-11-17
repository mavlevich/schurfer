# Stage 2C paper screens v1: I36, I35, I26/I27 and I45 at USD 5,000

Status: **paper screens, written 2026-11-17. No market data was read on any window:
no price, funding rate, open interest, spread, book snapshot, return or volume, from
any venue or archive. No request was made to any exchange or data service.** The only
inputs are repository documents, published fee and rate conventions (section "Inputs
and sources") and formulas. The HYP-012 v2 blind window from 2026-09-29 is closed to
every price read and was not approached. This document registers nothing, authorizes no
fetch, and is not a verdict on any strategy's profit: it says which forms are worth a
measurement and which can be stopped on arithmetic alone. It does not touch the
I25 protocol (PR #608, not yet in main) or the Stage 2A contract.

It continues the Stage 2C list of the [consilium plan](../consilium/plan.md), starts
from the I35 and I36 text in the [B screen](../consilium/screens/b-2026-11-15.md) and
the [ideas ledger](../consilium/ideas.md), and uses the same cost sheet as the
[A screen](../consilium/screens/a-2026-11-15.md).

## Inputs and sources

**Capital.** The owner fixed the working capital at **USD 5,000** on 2026-11-17
(plan section 1). Every hurdle below is on **all reserved capital**: both legs, margin,
buffer and idle cash.

**Hurdle.** The owner's reference for free USDT is **5% a year**, USD 250 a year on USD
5,000 (see the [Bybit carry economics screen](bybit-carry-economics-screen-v1.md), which
failed at every funding threshold H: best +0.3% a year in v1.1 and -4.9% to -36.1% in
v1.2 with liquidation priced, on USD 210, one slot, after 20 bps crossing). That result's main lesson is reused here: with
isolated collateral a pair ties up about 4.2 times its notional, so the **capital
ratio**, not the headline rate, decides the return. Every screen below therefore carries
an explicit capital parameter. I35 adds a 3 point premium (8% a year) for liquid-staking
token (LST) risk, as the B screen did; the 5% figure is also shown.

**Fees** (bps per side, base tier, non-VIP; regional schedules and the owner's account
tier were not checked):

| Item                              |                  Taker | Maker | Source                                                                                                                                                                 |
| --------------------------------- | ---------------------: | ----: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bybit USDT perpetual              |                    5.5 |   2.0 | [Bybit fees](https://www.bybit.com/en/help-center/article/Perpetual-Futures-Contract-Fees-Explained)                                                                   |
| Bybit USDC perpetual              |                    5.5 |   2.0 | same page; no settlement fee stated for USDC futures                                                                                                                   |
| Bybit spot                        |                   10.0 |  10.0 | [Bybit spot fees](https://www.bybit.com/en-US/help-center/s/article/Bybit-Spot-Fees-Explained), regional pages differ                                                  |
| Bybit USDT dated (expiry) futures |                    5.5 |   2.0 | same perpetual page; whether a settlement fee applies to USDT expiry contracts is stated inconsistently, so **2.5 bps at expiry is added as a conservative allowance** |
| Lido protocol fee                 | 10% of staking rewards |       | [Lido protocol fee](https://lido.fi/how-lido-works/protocol-fee) (DAO can change it)                                                                                   |

Non-fee cost per leg per round trip (spread, impact): the program models 19 bps for
established alts at USD 50 ([A screen](../consilium/screens/a-2026-11-15.md)). It is not
measured for BTC, ETH or USDC books at USD 5,000. This document uses **x = 3 bps
(favorable), 10 bps (conservative) for BTC/ETH legs** and keeps the program's 19 for alt
legs. These are assumptions, labelled as such, not readings.

**Funding convention** ([Bybit funding](https://bybit.nl/en/help-center/article/What-is-funding-rate-and-predicted-rate)):
F = P + clamp(I - P, -0.05%, +0.05%), interest I = 0.03% a day (0.01% per 8 h), so at
zero premium a short earns **0.01% per 8 h = 10.95% a year on notional**. Some pairs
default to I = 0 (for example USDCUSDT, ETHBTCUSDT); whether USDC-margined perpetuals
do is **not checked**. The realized average funding of any contract was not read.

**Power formula** (as in the [BTC trend feasibility](btc-trend-feasibility-v1.md)): MDE
of a mean at one-sided 5% and 80% power is **2.487 standard errors**; for weekly
returns with spread `sd` over `W` weeks, the yearly MDE is `2.487 * sd * 52 / sqrt(W)`,
that is `17.9 * sd` for the 52 discovery weeks and `14.0 * sd` for the 85 holdout weeks.
Weekly spreads of the baskets below are **assumptions, not measurements**.

## Common verdict rule (fixed now, before any measurement)

Each form is computed at a **favorable corner** and a **conservative corner** of its
stated input bounds.

- `screen_fail`: the favorable corner misses the hurdle, or misses the power condition
  the program requires of any candidate (a lower bound of net EV above zero on an
  untouched window).
- `screen_pass`: the conservative corner clears the hurdle with a stated margin. It
  permits writing a protocol, never an order, and is not evidence of a return.
- `needs_measurement`: the favorable corner clears and the conservative corner does not.

A screen that rests on an assumed market quantity says so and names the cheapest
measurement that could reverse it. Windows for any later read are the discovery year
(2024-10-01 to 2025-09-30) only; the backward holdout (2023-02-01 to 2024-09-15) is
spent once, as one pre-registered batch, and no screen here asks for it.

## I36: cross-sectional funding carry (Bybit perpetuals)

**Mechanism.** Hold a dollar-neutral basket: short the k perpetuals with the highest
settled funding, long the k with the lowest, rebalanced slowly; the crowded side of each
perpetual pays the other. The edge is the funding spread, and its enemy is price drift
of the shorted squeezes against the longs.

**Fixed parameters.** Bybit USDT linear perpetuals only (Hyperliquid's cost is
unmeasured and would add a second venue); k = 5 per side; rank by the trailing 7-day sum
of settled funding; weekly rebalance on Monday 00:00 UTC; taker execution, alt legs at
x = 19 bps (conservative) or 5 bps (favorable, the most liquid decile only); funding of
both legs counted with sign.

**Arithmetic.** Let `rho` be reserved capital per dollar of gross notional, `tau` the
weekly share of the basket replaced, `leg` the round-trip cost of one leg (fees 11 bps
plus `x`). Per-side notional is `N = C / (2 rho)`.

| Corner       |  rho | N per side |  tau | leg, bps | Hurdle, % of N | Turnover drag, % of N | Required spread `s`, % of N a year | Same, bps a day |
| ------------ | ---: | ---------: | ---: | -------: | -------------: | --------------------: | ---------------------------------: | --------------: |
| Conservative | 0.50 |      5,000 | 0.25 |       30 |           5.00 |                  7.80 |                              12.80 |            3.51 |
| Favorable    | 0.25 |     10,000 | 0.10 |       16 |           2.50 |                  1.66 |                               4.16 |            1.14 |

Turnover drag is `52 * tau * 2 legs * leg`. For scale, the carry screen's median 24 h
funding was 2.55 bps a day (about 9% a year) with 78% of settlements positive: the
basket's held spread must be 1.1 to 3.5 bps a day **after the ranks decay**, which the
screen cannot say. The favorable corner has a second problem: at `rho = 0.25` the per
side notional is twice the capital, and one shorted name squeezing to 4x within a week
(the carry screen saw 4x and 7x moves) loses 3x its fifth of the short side, about 1.2
times the capital, so that corner would be liquidated by a case already seen on record.

**Power.** To see an effect of size `s` the weekly basket spread must satisfy
`sd <= s / 17.9` on 52 weeks or `s / 14.0` on 85 weeks:

| Corner       |    `s` | max `sd` per week, 52 weeks | max `sd` per week, 85 weeks |
| ------------ | -----: | --------------------------: | --------------------------: |
| Conservative | 12.80% |                       0.71% |                       0.91% |
| Favorable    |  4.16% |                       0.23% |                       0.30% |

The BTC trend note already used a weekly BTC spread of about 5.5% (2.77% at p = 0.5)
and found that 0.71% was a quarter of it. A ten-name dollar-neutral basket of alts is not
plausibly an order of magnitude quieter than BTC. Even at a deliberately generous
weekly `sd = 1%`, the 85-week holdout cannot detect less than 14% a year and the 52-week
year less than 18%, against a favorable requirement of 4.16%.

**Rule fixed in advance.** `screen_fail` on power if the favorable corner needs a weekly
spread under 0.5% on the 85-week holdout. It does (0.30%). It reverses only if a
dispersion-only read (spread of the weekly basket difference, no mean, no net) of
the discovery year shows `sd <= 0.30%`.

**Data and request budget of a real test.** None is requested. For reference: the local
carry snapshot (287 live spot-perp pairs, sha256 prefix `24a7f03b`, funding and 4 h
klines, 2024-10-01 to 2026-09-28) can be re-read with zero requests but has survivorship
(live pairs only) and has already been read at the outcomes level for the carry screen,
so it is discovery-only. Adding delisted perpetuals costs, per symbol and year, about 6
funding requests (200 rows each, 1,095 rows) plus 3 klines requests (1,000 bars each);
150 symbols are about 1,350 requests, roughly 23 minutes at one request a second. The
holdout would cost about 13 requests per symbol, near 5,000 requests for 400 symbols,
and is not asked for.

**What this screen did not measure.** Any funding rate, rank persistence, spread of
funding between the tails, price drift or basket volatility; capacity at USD 5,000
(depth of alt perps); margin mode and cross-margin netting on Bybit; liquidation risk
beyond the one squeeze case quoted from an earlier report; the Hyperliquid leg.

**Verdict: `screen_fail` (power).** Changes the B screen's `needs_measurement`: the B
screen stopped on the same condition ("MDE above the realistic net spread") but left it
open for the measurement; here the condition is evaluated on paper with stated bounds.
It rests on the assumed floor `sd >= 1%` a week and is the first item for the colleague.

## I35: ETH staking carry with a short-perp hedge

**Mechanism.** Hold an LST (stETH, wstETH or similar) and short ETHUSDT perpetual of
equal notional: the staking yield is earned, the ETH price is hedged, and the short
usually receives funding. Risks that remain: LST discount to ETH, smart-contract and
slashing risk, withdrawal queue, and funding turning negative.

**Fixed parameters.** One position of about USD 5,000 of reserved capital; `alpha = N /
C`, the share of capital in the hedged notional; staking yield after the protocol fee
`y`; net funding on the short `f`; amortized entry and exit cost `c` per year of
holding. Net return on all capital: `alpha * (y + f - c) + (1 - alpha) * 5%`, where
the idle part earns the 5% reference.

**Correction of the B screen.** The B screen derives that `y + f - c` must exceed 11.4%
for an 8% target at `alpha = 0.7`. That drops the idle term its own formula contains:
the correct requirement is `5% + 3% / alpha = 9.29%`. The B screen also reads its
"funding was not seen" claim from the carry screen. That screen's negative result came
from an isolated 4.2x collateral ratio and rule churn, **not** from a measured ETH
funding rate, so it says nothing about `f` here; this screen does not lean on it.

**Arithmetic.** Required net funding `f = 5% + (H - 5%) / alpha - y + c`:

| Corner       | alpha | y, % | c, % | `f` needed at H = 8% | `f` needed at H = 5% | `f` needed at H = 8%, share of the 10.95% convention |
| ------------ | ----: | ---: | ---: | -------------------: | -------------------: | ---------------------------------------------------: |
| Conservative |  0.50 |  2.5 |  1.0 |                 9.50 |                 3.50 |                                                  87% |
| Middle       |  0.70 |  3.0 |  0.5 |                 6.79 |                 2.50 |                                                  62% |
| Favorable    |  0.90 |  3.5 | 0.25 |                 5.08 |                 1.75 |                                                  46% |

Inputs: `y` is a staking yield band of 2.5% to 3.5% after the 10% Lido fee (the B
screen used 3 to 3.5%); it is an assumption, no live rate was read. `c` amortizes, over
one year of holding, about 100 bps conservative (spot 2 x (10 + 10), perp 11 + 10, gas
and swap slippage near 20 bps each way on USD 5,000) or 25 bps favorable (spot fees 20,
passive perp 4, no gas, which needs the LST to trade on the exchange). `alpha` of 0.9
needs the LST accepted as perpetual collateral at a high ratio; whether the owner's
account does this is **not verified**, and 0.5 is the case where the buffer is held in
USDT.

In dollars at USD 5,000, each point of net `y + f - c` above the requirement is worth
USD 25 (`alpha = 0.5`) to USD 45 (`alpha = 0.9`) a year; at the favorable corner the
excess over idle USDT is a few tens of dollars a year at the 8% hurdle.

**Rule fixed in advance.** `screen_pass` needs the conservative `f` at most half of the
convention (5.5% a year); it is 9.5%, so no pass. `screen_fail` needs the favorable `f`
above the convention itself; it is 5.1%, so no fail. Hence `needs_measurement`.

**Data and request budget of the measurement.** Settled funding of ETHUSDT on the
discovery year: 1,095 rows, 6 requests, plus 3 requests for 4 h klines; under 20
requests with a retry reserve. The decision quantity is the mean annualized funding
and the worst rolling 90 day mean, not a statistic with a power claim: one year of one
contract is a small number of funding regimes, so this is a stress check, never a
confirmation. The LST discount history is a separate source and may not exist inside the
allowed windows.

**What this screen did not measure.** The actual ETH funding average or any regime of it;
current staking yield; LST discount history; whether Bybit lists the LST as collateral and
at what ratio, or whether staking can be done from Poland; gas cost; tax; slashing,
withdrawal-queue and contract risk (carried only by the 3 point premium, which is a
choice, not an estimate).

**Verdict: `needs_measurement`.** Supersedes the B screen's `paper_fail`, which rested on
the arithmetic slip and on the misattributed funding evidence above. Weak prior: the
conservative case needs 87% of the zero-premium funding rate, and the carry screen on this
venue (a different structure) was negative.

## I26: dated BTC and ETH futures basis (long spot, short the dated future)

**Mechanism.** Buy spot and sell the dated (quarterly) future; the basis converges to
zero at expiry, so the return is fixed at entry up to execution and settlement basis,
and is paid by leveraged longs for the delay.

**Fixed parameters.** BTC and ETH only; one position per asset at a time, held to expiry;
entry at a fixed days-to-expiry, one cycle about a quarter, so about four cycles a year;
cost per cycle, with `x` the non-fee cost of a leg round trip (10 conservative, 3
favorable, so 5 or 1.5 per crossing): conservative **43 bps** (spot in and out 10 + 10,
future in 5.5, expiry allowance 2.5, three crossings at 5) and favorable **27 bps**
(spot fees 20, passive future entry 2.0, no settlement fee, crossings 4.5).

**Arithmetic.** `alpha = N / C`. With the spot leg outside the collateral pool the short
needs a buffer `B = R * V` for a rally `R` before expiry, so `alpha = 1 / (1 + R)`: 0.67 at
R = 50%, 0.50 at R = 100%. With the spot counted as collateral, `alpha` can reach 0.9
(not verified). Annualized entry basis `a` on notional is required to satisfy
`alpha * (a - 4c) + (1 - alpha) * 5% >= 5%`, that is `a >= 5% + 4c`:

| Corner       | cost per cycle `c` | required annualized basis `a` |
| ------------ | -----------------: | ----------------------------: |
| Conservative |             43 bps |                          6.7% |
| Favorable    |             27 bps |                          6.1% |

Dollars on USD 5,000 at `a = 10%`: net 6.6% (conservative cost, `alpha = 0.5`) to 8.5%
(favorable cost, `alpha = 0.9`), an excess over idle USDT of about USD 80 to 175 a year;
at `a = 8%` USD 30 to 85. Each point of basis above the
requirement is worth USD 25 to 45 a year. This is the scale of the whole idea at the
owner's size: **a permission to build the process, not income.**

**Rule fixed in advance.** The favorable corner needs `a >= 6.1%` and the conservative
one `a >= 6.7%`; whether the Bybit quarterly basis exceeds that is the unmeasured
fact. A measurement passes only if, for each of BTC and ETH, at least half of the
discovery-year cycles quote `a >= 6.7%` at the fixed entry and none is below zero. With
four cycles per asset these are counts, not statistics.

**Data and request budget of the measurement.** First, coverage with no price: whether
Bybit's dated contracts of the discovery year appear in the existing linear
`instruments-info` catalog (statuses Trading, Delivering and Closed, kept by the
catalyst fetch); zero requests if present. Then 4 h klines of each dated contract
(about 720 bars, one request each) and BTC and ETH spot or index klines (3 requests per
symbol year): about 20 requests in total. Nothing here needs the holdout.

**What this screen did not measure.** Whether Bybit lists these dated contracts for the
owner's account, their size minimums, any basis or its regime, the delivery price rule
(an index average over the last 30 minutes for USDC futures; not checked for USDT) and
the residual basis when selling spot against it, margin treatment of spot, and the
conflicting statements about a settlement fee.

**Verdict: `needs_measurement`.** The mechanism has a fixed payoff and needs no
statistical proof, but the idea's own caveat stands: it exists when the basis is above
the hurdle, which is a market regime, and it ties up most of the capital.

## I27: USDT perpetual against USDC perpetual of the same asset on Bybit

**Mechanism.** Long one and short the other of two same-asset perpetuals priced off one
index; the premium of the thinner USDC book, and any difference in funding, are the
effect. Price risk is hedged; the exposure left is the USDT/USDC rate and the margin
currency.

**Fixed parameters.** BTC and ETH only; four fills. Conservative: taker in and out on
both legs, 22 bps of fees plus `x = 10` per leg round trip, **42 bps** a cycle.
Favorable: maker in and out on both legs, 8 bps of fees plus `x = 3` per leg, **14 bps**
a cycle, assuming every passive order fills with no adverse selection. Held 30 days or
90 days. Capital: `N = C / (2 rho)` as in I36.

**Arithmetic.** Net on `N` is `d - c * 365 / hold`, where `d` is the persistent annual
differential captured on notional, so `d = 250 / N + c * 365 / hold`:

| Corner       | N per side | cost | hold    | required `d`, % a year | bps a day | bps per 8 h |
| ------------ | ---------: | ---: | ------- | ---------------------: | --------: | ----------: |
| Conservative |      5,000 |   42 | 30 days |                  10.11 |      2.77 |        0.92 |
| Conservative |      5,000 |   42 | 90 days |                   6.70 |      1.84 |        0.61 |
| Favorable    |     10,000 |   14 | 90 days |                   3.07 |      0.84 |        0.28 |
| Favorable    |     10,000 |   14 | 30 days |                   4.20 |      1.15 |        0.38 |

So the pair must hold a differential of 0.3 to 0.9 bps per 8 h net of costs, for months,
between two contracts on one index that every market maker on the venue can also trade.
The favorable corner is not impossible on paper and is not argued away.

**Rule fixed in advance.** Measure the funding of the paired contracts only. Choose the
side by the trailing 7-day mean funding difference; the next 7 days' realized funding
difference on notional, annualized, must exceed **3.1% a year** (the favorable corner)
in the median walk-forward week, else `screen_fail` on the measurement. Passing it
leads only to a cost-only book measurement, never to a trade.

**Data and request budget of the measurement.** Funding history of 4 contracts (BTC and
ETH on each margin currency): 6 requests per contract for the discovery year, plus 3
klines requests each, about 40 requests. The cost side needs Bybit's order book archive
(history from 2023-01-18 per the [strategy v2](research-strategy-v2.md) coverage check),
not requested here, and would be one cost-only contract shared with Stage 2A's method.

**What this screen did not measure.** The funding or the price gap of either contract;
the interest component of USDC perpetuals; the cost of converting USDT into USDC;
depth of USDC books at USD 5,000; USDT/USDC depeg risk; whether USDC margining is
open to the owner's account; maker fill odds and adverse selection (the favorable
14 bps assumes none).

**Verdict: `needs_measurement`, low prior.** Cheap to measure, small by the arithmetic,
and exposed to the argument that it is the most arbitraged structure on the list.

## I45: intraday BTC and ETH, continuation or mean reversion

**Mechanism.** A pre-defined state (a large move, compression or breakout) is followed
on average by continuation or by reversion on the deepest perpetuals, held a few hours.
The two directions are symmetric in this arithmetic; **if any measurement is ever
authorized, one direction must be chosen before returns are read**, and this screen
chooses neither.

**Fixed parameters.** BTCUSDT and ETHUSDT perpetual, long or short, one position at a
time, notional equal to `C = USD 5,000` (no leverage), at most 730 entries a year (two a
day), hold `h` of 1, 2, 4, 8 or 12 hours. The daily horizon is not part of this screen
(it is the [BTC trend](btc-trend-feasibility-v1.md) question). Annualized price volatility
`sigma = 45%` central, band 30% to 60% (an assumption band, **not read**), normal
returns, so the spread of an `h` hour return is `sd = sigma * sqrt(h / 8760)` and the
mean absolute move is `0.798 * sd`.

**Arithmetic.** A directional rule with sign accuracy `p` earns gross `g = (2p - 1) *
0.798 * sd` per trade. Costs per round trip, bps: **14** favorable taker (fee 11 + x 3),
**30** the program's taker wall, **5** favorable maker (fees 4 + 1, assumes both legs
fill at the touch). Hurdle on all capital: `730 * (g - c) >= 500 bps`, so `g >= c +
0.7 bps` only; the cost wall, not the 5%, binds. To be **detectable** on one discovery
year with design effect 1.2 the effect must reach the MDE `2.487 * sd / sqrt(730 / 1.2)`.
Required sign accuracy (central `sigma = 45%`):

| `h`, hours | `sd`, bps | MDE, bps | `p` to break even at 14 bps | `p` to be detectable at 14 bps | at 30 bps | maker, at 5 bps |
| ---------: | --------: | -------: | --------------------------: | -----------------------------: | --------: | --------------: |
|          1 |      48.1 |      4.8 |                       69.2% |                          74.6% |     95.4% |           62.8% |
|          2 |      68.0 |      6.9 |                       63.5% |                          69.2% |     84.0% |           60.9% |
|          4 |      96.2 |      9.7 |                       59.6% |                          65.4% |     75.9% |           59.6% |
|          8 |     136.0 |     13.7 |                       56.8% |                          62.8% |     70.1% |           58.6% |
|         12 |     166.6 |     16.8 |                       55.5% |                          61.6% |     67.6% |           58.2% |

At the most favorable corner of the band (`sigma = 60%`, 14 bps) the detectable
requirement is 70% at 1 h, 63% at 4 h, 61% at 8 h and 60.3% at 12 h, still above 60% at
every `h`. A
design effect of 3 (events cluster in volatile days) raises every MDE by about 60%.

**Rule fixed in advance.** `screen_fail` for the taker forms if, at the favorable
corner of the volatility band and 14 bps, the sign accuracy needed to be detectable on
the discovery year exceeds **60%** at every `h`. A 10 point edge over a coin flip on
BTC or ETH at an hour to half a day, net of a fee wall, would be an extraordinary
signal; the table lets a reader apply a different ceiling. It does: 60.3% to 70%.

**Data and request budget of a real test.** None is requested. For reference: hourly
klines for the discovery year, about 9 requests per symbol (possibly zero, if the HYP-032
features snapshot is reused); 1 minute klines about 526 requests per symbol-year, 1,052
for both. The holdout is not asked for.

**What this screen did not measure.** Real volatility, autocorrelation or any state
conditioned effect; the actual cost of BTC and ETH books at USD 5,000 (assumed x = 3 to
10); maker fill probability and adverse selection (a passive fill is more likely when
the price is moving against the order); funding during holds; market exposure of a
persistent long bias; the clustering of events (the design effect is assumed, and the
HYP-037 calibration found design effects from 2.66 to 111 on other event families).

**Verdict: `screen_fail` for taker execution at every horizon up to 12 h.** The passive
form at 4 to 12 h stays under the 60% ceiling (58% to 60%) but depends on a fill model
the repository does not have, so it is `needs_measurement`, **not recommended**:
the figure assumes no adverse selection.

## Summary

| Idea | Verdict                                      | Decided by                                                                   |
| ---- | -------------------------------------------- | ---------------------------------------------------------------------------- |
| I36  | `screen_fail` (power)                        | Required spread 4.2% to 12.8% of notional needs weekly sd under 0.3% to 0.9% |
| I35  | `needs_measurement`                          | Net ETH funding must reach 5.1% to 9.5% a year; convention is 10.95%         |
| I26  | `needs_measurement`                          | Annualized basis must reach 6.1% to 6.7%; excess over idle USD 80 to 175     |
| I27  | `needs_measurement`, low prior               | Paired-contract funding difference must reach 3.1% to 10.1% a year           |
| I45  | `screen_fail` (taker); maker not recommended | Sign accuracy 62% to 75% needed to be detectable; the cost wall binds        |

None of the four is a candidate for the single backward-holdout exam as it stands.
The cheapest next facts, all under 100 requests and all inside the discovery year, are
the ETH funding mean (I35), dated-contract coverage and entry basis (I26) and the paired
funding difference (I27). None is authorized by this document; each needs the owner's
separate go and its own contract, and I35 and I27 should wait for the I25 and Stage 2A
decisions so they do not consume attention or a data budget the first two lines need.

## Not measured, in one place

Every funding rate, price, spread, depth, basis, volatility and return; account tier and
regional fees; the owner's access to dated futures, USDC perpetuals and LST collateral;
margin and cross-collateral rules in the owner's account; cost of USD 5,000 against
real books; the delivery price rule of USDT expiry contracts; the actual staking yield.
All numeric bands above that are labelled assumptions come from the stated sources and
earlier repository reports, not from a reading.

## Open questions for the colleague

1. **I36 power verdict.** Is it acceptable to call `screen_fail` on an assumed floor of
   1% weekly spread (needed 0.30% or less), or should it stay `needs_measurement` until
   a dispersion-only read of the existing snapshot? The B screen chose the latter.
2. **I35 correction.** Do you agree the B screen's 11.4% requirement was a slip (9.29% at
   `alpha = 0.7`) and its use of the carry result was a misattribution, so `paper_fail`
   is withdrawn? Is the 3 point LST premium still right, or should I35 face 5%?
3. **Capital ratio.** Is `alpha` 0.5 to 0.9 and `rho` 0.25 to 0.5 the right range for a
   Bybit unified account, and may the owner count spot or an LST as collateral? This
   moves I26, I35 and I36 more than any rate.
4. **Cost bands.** x = 3 to 10 bps for BTC and ETH legs at USD 5,000 is my assumption;
   the only measured books in the repository are Bybit at USD 50. Keep or replace?
5. **I45 ceiling.** Is 60% sign accuracy a defensible plausibility ceiling, and is a
   design effect of 1.2 acceptable here when HYP-037 found much larger ones?
6. **I26 and I27 order.** Both are small (tens of dollars). Should they wait behind I25
   and Stage 2A, or is the dated-contract coverage check (zero requests if the catalog
   has it) worth doing now?
7. **I27 maker form.** The 14 bps case assumes no adverse selection. Should it be dropped
   from the screen as unscreenable?
8. **Stop at 2027-02-11.** If I25 and Stage 2A both stop, none of these four is a
   candidate that can reach an exam; is the right outcome to say so now?
