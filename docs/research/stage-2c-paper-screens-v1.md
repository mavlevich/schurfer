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

**Capital split and interest on the buffer.** Reserved capital `C` is split into the
hedged leg (spot or LST, value `N`), **margin** for the short (assumed 10% of `N`, not
checked against Bybit's tiers), **buffer** (the rest of what is held back against a
rally of the short), and **free** USDT. Only free USDT is certain to earn the 5%
reference. Whether margin or buffer left on the exchange also earns interest at the same
time is **not confirmed**. The conservative corner therefore credits **zero interest on
margin and buffer** (`r_b = 0`); the favorable corner credits 5% (`r_b = 5%`), which
needs the account to pay interest on unused collateral. Every dollar figure below states
which of the two it uses. This is the first account fact to check before any measurement
of I26 or I35: it is a documentation question and costs no request.

**Fees** (bps per side, base tier, non-VIP; regional schedules and the owner's account
tier were not checked):

| Item                              |                  Taker | Maker | Source                                                                                                                                                                                                                                                  |
| --------------------------------- | ---------------------: | ----: | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bybit USDT perpetual              |                    5.5 |   2.0 | [Bybit fees](https://www.bybit.com/en/help-center/article/Perpetual-Futures-Contract-Fees-Explained)                                                                                                                                                    |
| Bybit USDC perpetual              |                    5.5 |   2.0 | same page; no settlement fee stated for USDC futures                                                                                                                                                                                                    |
| Bybit spot                        |                   10.0 |  10.0 | [Bybit spot fees](https://www.bybit.com/en-US/help-center/s/article/Bybit-Spot-Fees-Explained), regional pages differ                                                                                                                                   |
| Bybit USDT dated (expiry) futures |                    5.5 |   2.0 | same perpetual page; official pages disagree between 0 and 5 bps for a settlement fee at expiry, so **two scenarios are used: 0 bps (favorable) and 5 bps (conservative)** until the specific contract is clarified; the midpoint is not an upper bound |
| Lido protocol fee                 | 10% of staking rewards |       | [Lido protocol fee](https://lido.fi/how-lido-works/protocol-fee) (DAO can change it)                                                                                                                                                                    |

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

- `screen_fail`: the favorable corner misses the hurdle on the arithmetic of the form
  itself. A power shortfall at an **assumed** variance is never by itself a
  `screen_fail`: the required spread is a minimum for the economics, not an upper bound
  on the real effect, and an MDE above it does not exclude a larger effect. Such a form
  is `needs_measurement` (of the variance) or is `deferred_by_priority`, a label that
  says it is stopped by budget and order of work and not by a power failure.
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

The hurdle here credits no interest on margin or buffer: all of `C` is reserved, so the
5% is charged on all of it (the conservative reading of the buffer rule above).

**Power, stated for what it is.** The required `s` is the **smallest** spread that makes
the economics work; it is not a ceiling on what the strategy may earn. A test whose MDE
is above `s` cannot confirm an effect of exactly `s`, but it does not exclude a larger
one, so it does not stop the idea. What power can say is conditional on the variance of
the quantity actually tested: the weekly **net** basket return (funding collected, price
drift of both legs, turnover cost), whose own spread is unmeasured. With that spread
`sd` the yearly MDE is `17.9 * sd` on 52 weeks and `14.0 * sd` on 85 weeks, so the
following weekly `sd` makes an effect of exactly `s` just detectable:

| Corner       |    `s` | `sd` at which MDE = `s`, 52 weeks | `sd` at which MDE = `s`, 85 weeks |
| ------------ | -----: | --------------------------------: | --------------------------------: |
| Conservative | 12.80% |                             0.71% |                             0.91% |
| Favorable    |  4.16% |                             0.23% |                             0.30% |

The BTC trend note used a weekly BTC spread of about 5.5%. If the net basket spread were
as low as 1% a week (a generous assumption for an alt basket), the 85-week holdout could
confirm only effects of 14% a year or more and the 52-week year 18% or more. That is a
statement about **what one window can confirm**, not about what the strategy earns: an
edge between 4.16% and 14% a year would be economically enough and statistically
invisible on one window. A clean test would need the spread of the net return, measured,
and a longer or pooled sample. The funding component alone is nearly deterministic once
the ranks are fixed, and its own variance is small; the price-drift component carries
the risk and the variance.

**Rule fixed in advance.** No `screen_fail` on power. The form is `needs_measurement` of
one quantity: the spread of the weekly net basket return on the discovery year (a
dispersion read: variance, no mean, no net return, no label), which converts the
assumed `sd` into a measured one. It is **deferred by priority** behind I25 and Stage 2A,
which is a decision about budget and order and not a finding of this arithmetic.

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

**Verdict: `needs_measurement`, `deferred_by_priority`.** Same as the B screen. The
first version of this document called it a power `screen_fail`; review showed that the
required spread is a minimum and not an upper bound, and that power must be computed for
the net return with its own, unmeasured variance. What does stand on the arithmetic: the
required spread is 4.2% to 12.8% of notional a year after turnover, the favorable corner
(`rho = 0.25`) would be liquidated by a squeeze already on record, and the conservative
corner needs 3.5 bps a day to persist after the ranks decay.

## I35: ETH staking carry with a short-perp hedge

**Mechanism.** Hold an LST (stETH, wstETH or similar) and short ETHUSDT perpetual of
equal notional: the staking yield is earned, the ETH price is hedged, and the short
usually receives funding. Risks that remain: LST discount to ETH, smart-contract and
slashing risk, withdrawal queue, and funding turning negative.

**Fixed parameters.** One position of about USD 5,000 of reserved capital; `alpha = N /
C`, the share of capital in the hedged notional; staking yield after the protocol fee
`y`; net funding on the short `f`; amortized entry and exit cost `c` per year of
holding. Net return on all capital: `alpha * (y + f - c) + (1 - alpha) * r_b`, where
`r_b` is the interest earned on everything that is not the hedged leg (margin, buffer
and free USDT; see the capital split above). The target `H` is 8% (5% plus the 3 point
premium) or 5%.

**Correction of the B screen, and its limit.** The B screen derives that `y + f - c` must
exceed 11.4% for an 8% target at `alpha = 0.7`. That is `H / alpha`, the requirement
when the leftover capital earns **nothing**; with leftover capital earning 5% the
requirement is `5% + 3% / alpha = 9.29%`. Neither is a slip: they are two assumptions
about `r_b`, and an earlier version of this document wrongly called 11.4% an error. The
9.29% figure holds **only if** the leftover capital earns interest, which is not
confirmed. The B screen also reads its "funding was not seen" claim from the carry
screen. That screen's negative result came from an isolated 4.2x collateral ratio and
rule churn, **not** from a measured ETH funding rate, so it says nothing about `f` here;
this screen does not lean on it.

**Arithmetic.** Required net funding `f = (H - (1 - alpha) * r_b) / alpha - y + c`.
Capital split in dollars (margin is 10% of `N`, buffer the rest of the leftover, free
USDT zero, so in the conservative and middle rows every leftover dollar is held against
the short):

| Corner       | alpha | LST `N` | margin | buffer | free | `r_b` | y, % | c, % | `f` at H = 8% | `f` at H = 5% | H = 8%, share of the 10.95% convention |
| ------------ | ----: | ------: | -----: | -----: | ---: | ----: | ---: | ---: | ------------: | ------------: | -------------------------------------: |
| Conservative |  0.50 |   2,500 |    250 |  2,250 |    0 |    0% |  2.5 |  1.0 |         14.50 |          8.50 |                                   132% |
| Middle       |  0.70 |   3,500 |    350 |  1,150 |    0 |    0% |  3.0 |  0.5 |          8.93 |          4.64 |                                    82% |
| Middle       |  0.70 |   3,500 |    350 |  1,150 |    0 |    5% |  3.0 |  0.5 |          6.79 |          2.50 |                                    62% |
| Favorable    |  0.90 |   4,500 |    450 |     50 |    0 |    5% |  3.5 | 0.25 |          5.08 |          1.75 |                                    46% |

The two middle rows are the same position under the two interest assumptions. With
interest on the leftover the conservative corner would need 9.50% (H = 8%) and 3.50%
(H = 5%), and the favorable corner without interest 5.64%.

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
convention (5.5% a year); it is 14.5% (9.5% with interest on the leftover), so no pass.
`screen_fail` needs the favorable `f` above the convention itself; it is 5.1% (5.6%
without interest), so no fail. Hence `needs_measurement`. The conservative corner
needing more than the convention says that the result depends on two account facts
(interest on unused collateral, LST accepted as collateral) before it depends on the
funding rate.

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
the misattributed funding evidence above (its 11.4% is the no-interest case, not a
slip). Weak prior: the conservative case needs more than the whole zero-premium funding
rate (132% at H = 8%, 78% at H = 5%), the middle case 82% without interest on the
leftover, and the carry screen on this venue (a different structure) was negative.

## I26: dated BTC and ETH futures basis (long spot, short the dated future)

**Mechanism.** Buy spot and sell the dated (quarterly) future; the basis converges to
zero at expiry, so the return is fixed at entry up to execution and settlement basis,
and is paid by leveraged longs for the delay.

**Fixed parameters.** BTC and ETH only; one position per asset at a time, held to expiry;
entry at a fixed days-to-expiry, one cycle about a quarter, so about four cycles a year;
cost per cycle, with `x` the non-fee cost of a leg round trip (10 conservative, 3
favorable, so 5 or 1.5 per crossing): conservative **45.5 bps** (spot in and out 10 + 10,
future in 5.5, expiry fee 5, three crossings at 5) and favorable **26.5 bps** (spot fees
20, passive future entry 2.0, expiry fee 0, crossings 4.5). The expiry fee scenarios are
5 and 0 bps (see the fee table); an earlier version used 2.5 and costs of 43 and 27.

**Arithmetic.** `alpha = N / C`. With the spot leg outside the collateral pool the short
needs a buffer `B = R * N` for a rally `R` before expiry, so `alpha = 1 / (1 + R)`: 0.67 at
R = 50%, 0.50 at R = 100%; at `alpha = 0.5` there is **no free USDT**, the whole
leftover is margin and buffer (USD 2,500: margin 250, buffer 2,250). With the spot counted as collateral, `alpha` can reach 0.9
(not verified): USD 4,500 spot, 450 margin, 50 buffer. Annualized entry basis `a` on
notional, four cycles a year, return on all capital `alpha * (a - 4c) + (1 - alpha) * r_b`.
Required `a >= 4c + (5% - (1 - alpha) * r_b) / alpha`:

| Corner                           | alpha | `r_b` | cost per cycle `c` | required annualized basis `a` |
| -------------------------------- | ----: | ----: | -----------------: | ----------------------------: |
| Conservative                     |  0.50 |    0% |           45.5 bps |                        11.82% |
| Conservative, interest on buffer |  0.50 |    5% |           45.5 bps |                         6.82% |
| Favorable                        |  0.90 |    5% |           26.5 bps |                         6.06% |
| Favorable, no interest           |  0.90 |    0% |           26.5 bps |                         6.62% |

With the earlier cost of 43 bps the conservative figures were 11.72% without and 6.72%
with interest on the buffer; the earlier version of this document printed only the
interest case (6.7%). The 6.7% holds **only if** the buffer earns the 5% at the same time
as it serves as margin, which is not confirmed.

Dollars on USD 5,000 against idle USDT (USD 250 a year): at `a = 10%` the conservative
corner nets 4.09% (USD 204, **USD 46 below idle**), the conservative corner with
interest on the buffer 6.59% (USD 80 above), the favorable corner 8.55% (USD 177 above);
at `a = 8%` the same corners are USD 96 below, USD 30 above and USD 87 above. Each point
of basis above the requirement is worth USD 25 (`alpha = 0.5`) to 45 (`alpha = 0.9`) a
year. This is the scale of the whole idea at the owner's size: **a permission to build
the process, not income.**

**Rule fixed in advance.** The threshold depends on one account fact, so it is stated for
both readings: `a >= 11.8%` if the buffer earns nothing, `a >= 6.8%` if it is confirmed to
earn 5% (conservative corner), and `a >= 6.1%` for the favorable corner. Whether the Bybit
quarterly basis exceeds that is the unmeasured fact. A measurement passes only if, for
each of BTC and ETH, at least half of the discovery-year cycles quote `a` above the
threshold of the confirmed reading at the fixed entry and none is below zero. With four
cycles per asset these are counts, not statistics.

**Data and request budget of the measurement.** First, coverage with no price: whether
Bybit's dated contracts of the discovery year appear in the existing linear
`instruments-info` catalog (statuses Trading, Delivering and Closed, kept by the
catalyst fetch); zero requests if present. Then 4 h klines of each dated contract
(about 720 bars, one request each) and BTC and ETH spot or index klines (3 requests per
symbol year): about 20 requests in total. Nothing here needs the holdout.

**What this screen did not measure.** Whether Bybit lists these dated contracts for the
owner's account, their size minimums, any basis or its regime, the delivery price rule
(an index average over the last 30 minutes for USDC futures; not checked for USDT) and
the residual basis when selling spot against it, margin treatment of spot, whether
margin and buffer earn interest, and the conflicting statements about a settlement fee
(0 or 5 bps).

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
differential captured on notional, so `d = 250 / N + c * 365 / hold`. The hold is the
time between re-openings, so a weekly side decision with full turnover is a hold of 7
days (`365 / 7 = 52` cycles). The hurdle on notional is 5.0% at `N` = 5,000 and 2.5% at
`N` = 10,000 (no interest on margin credited):

| Corner       | N per side | cost | hold            | required `d`, % a year | bps a day | bps per 8 h |
| ------------ | ---------: | ---: | --------------- | ---------------------: | --------: | ----------: |
| Conservative |      5,000 |   42 | 7 days (weekly) |                  26.84 |      7.35 |        2.45 |
| Conservative |      5,000 |   42 | 30 days         |                  10.11 |      2.77 |        0.92 |
| Conservative |      5,000 |   42 | 90 days         |                   6.70 |      1.84 |        0.61 |
| Favorable    |     10,000 |   14 | 7 days (weekly) |                   9.78 |      2.68 |        0.89 |
| Favorable    |     10,000 |   14 | 30 days         |                   4.20 |      1.15 |        0.38 |
| Favorable    |     10,000 |   14 | 90 days         |                   3.07 |      0.84 |        0.28 |

So the pair must hold a differential of 0.3 to 0.9 bps per 8 h net of costs (favorable,
long hold) up to 2.5 bps per 8 h (conservative, weekly), between two contracts on one
index that every market maker on the venue can also trade. The favorable corner at a
long hold is not impossible on paper and is not argued away.

**Rule fixed in advance.** The threshold must match the way the side is chosen, so the
rule is stated on the realized turnover. Measure the funding of the paired contracts
only. Choose the side by the trailing 7-day mean funding difference, re-decided weekly,
and record the share `q` of weeks in which the chosen side flips (a flip closes and
reopens the position; no flip means holding). The required differential is
`d(q) = 250 / N + 52 * q * c`: **9.78% a year** if every week turns over (`q = 1`,
favorable cost), 3.07% only if `q` is about 1/13 (a hold near 90 days). The median
walk-forward week's realized funding difference on notional, annualized, must exceed
`d(q)` at the favorable cost, else `screen_fail` on the measurement. Because a weekly
review with a 90-day threshold would mix two holding periods, the 3.1% figure of the
first version is **withdrawn** as the test; the weekly walk-forward is a **preliminary
diagnostic of persistence**, and a trade-form test with its own hold rule would be a
separate contract. Passing leads only to a cost-only book measurement, never to a trade.

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

**Arithmetic.** A directional rule earns gross `g = (2p - 1) * 0.798 * sd` per trade
**only under an assumption**: either the correctness of the call is uncorrelated with
the size of the move, or `p` is the **move-weighted** accuracy, the share of the absolute
move that the call gets right (`p = E[|R| * 1(correct)] / E|R|`). A plain hit rate is not
enough: a rule can guess small moves and miss large ones and earn less than the formula,
or the reverse. The table below is therefore a **conditional model**, not a bound on
every rule. Costs per round trip, bps: **14** favorable taker (fee 11 + x 3),
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

**Rule fixed in advance.** A plausibility screen, not a proof: the form is judged
**unlikely under the model** for taker execution if, at the favorable corner of the
volatility band and 14 bps, the move-weighted accuracy needed to be detectable on the
discovery year exceeds a chosen ceiling of **60%** at every `h`. The 60% is a chosen
threshold, not a measured limit: a 10 point edge over a coin flip on BTC or ETH at an
hour to half a day, net of a fee wall, would be an extraordinary signal, and the table
lets a reader apply a different ceiling. It does exceed it: 60.3% to 70%. The design
effect of 1.2 is an optimistic scenario (the HYP-037 calibration found much larger ones).

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

**Verdict: `needs_measurement`, low prior, `deferred_by_priority`.** The first version
issued a blanket `screen_fail` for taker execution; that does not follow from a
conditional model whose accuracy is a plain hit rate under an independence assumption,
so it is withdrawn. What the arithmetic does show: under the model, with accuracy at or
below the chosen 60% ceiling and costs at the favorable 14 bps, no taker horizon up to 12
h is detectable on one discovery year, and at the 30 bps wall none is close. A rule whose
accuracy concentrates on large moves is not excluded by this table. The passive form at
4 to 12 h stays under the ceiling (58% to 60%) but assumes no adverse selection and
needs a fill model the repository does not have: an optimistic scenario, **not
recommended** until an execution model exists.

## Summary

| Idea | Verdict                                   | Decided by                                                                                                        |
| ---- | ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| I36  | `needs_measurement`, deferred by priority | Required spread 4.2% to 12.8% of notional; variance of the net basket return unmeasured; no power claim           |
| I35  | `needs_measurement`, weak prior           | Net ETH funding must reach 5.1% to 14.5% a year (convention 10.95%); depends on interest on the leftover          |
| I26  | `needs_measurement`                       | Basis must reach 6.1% to 11.8% a year, depending on interest on the buffer; USD 46 below to 177 above idle at 10% |
| I27  | `needs_measurement`, low prior            | Funding difference must reach 3.1% to 26.8% a year, depending on turnover; weekly test is a diagnostic            |
| I45  | `needs_measurement`, low prior, deferred  | Conditional model: no taker horizon detectable under a chosen 60% ceiling; maker is an optimistic scenario        |

**Status after review.** There is no ready candidate among these four now, and none is a
candidate for the single backward-holdout exam as it stands; the stop date 2027-02-11
is unchanged. I36 is deferred by priority (not by a power failure) and I26 and I27 queue
behind I25 and Stage 2A. The cheapest next facts, all under 100 requests and all inside
the discovery year, are the ETH funding mean (I35), dated-contract coverage and entry
basis (I26) and the paired funding difference (I27), plus one dispersion read for I36.
None is authorized by this document; each needs the owner's separate go and its own
contract. Two account facts decide more than any rate and cost no request: whether
margin and buffer earn interest on the owner's account, and whether spot or an LST counts
as collateral.

## Not measured, in one place

Every funding rate, price, spread, depth, basis, volatility and return; account tier and
regional fees; the owner's access to dated futures, USDC perpetuals and LST collateral;
margin and cross-collateral rules in the owner's account; cost of USD 5,000 against
real books; the delivery price rule of USDT expiry contracts; the actual staking yield.
All numeric bands above that are labelled assumptions come from the stated sources and
earlier repository reports, not from a reading.

## Open questions

Answered by the review of 2026-11-17 and applied above: I36 stays `needs_measurement` or
is deferred by priority; the carry attribution is corrected and the 3 point LST premium
is kept for now; `alpha` and `rho` stay scenarios until the account is checked; the cost
range x = 3 to 10 bps is a sensitivity assumption; 60% is a chosen plausibility
threshold and a design effect of 1.2 an optimistic scenario; I26 and I27 queue after I25
and Stage 2A; the maker forms stay optimistic scenarios until an execution model
exists; there is no ready candidate now and the stop date stays 2027-02-11.

Still open:

1. Does the owner's Bybit account pay interest on margin and buffer left unused, and may
   spot or an LST count as collateral? This sets `r_b` and `alpha` for I26 and I35.
2. Does the Bybit settlement fee for USDT expiry contracts apply (0 or 5 bps) on the
   specific contract the owner would trade?
3. Which of the cheap measurements, if any, should wait for the owner's go after I25 and
   Stage 2A decide?

## Account facts checked, 2026-11-22

Status note only. No verdict above changes, no price, rate, return or book was read, and
no owner account was used. Everything below comes from Bybit's public pages and one
public, unauthenticated Bybit endpoint, read on 2026-11-22.

- **LST as collateral (open question 1, second half).** `stETH` is accepted as unified
  trading account (UTA) collateral: the public collateral endpoint lists a ratio of 0.98
  up to 2,700 `stETH`, then 0.95, 0.90, 0.80, 0.70 and 0.60 in higher tiers. `ETH` has the
  same first tiers; `USDT` is 1.0. `wstETH` is **not listed**. At the owner's size this is
  the 0.98 tier. The ratios apply in cross and portfolio margin; the screens above assume
  isolated margin, so any real run must restate the liquidation risk under cross margin.
  Bybit says it reviews these ratios from time to time.
- **Interest on unused margin and buffer (open question 1, first half).** No source shows
  a UTA paying interest on idle balances. Bybit's own opt-in route is `BYUSDT`: `USDT` held
  in Flexible Easy Earn, swapped into the UTA at a 100% collateral ratio, usable in cross
  and portfolio margin only (not isolated), yielding the base Easy Earn rate without the
  platform reward part (the help article's example is a base rate of 1.3% a year; the
  current rate was not read), with a possible swap fee. Whether the owner's account is
  eligible for Easy Earn is **not verified**.
- **Effect on I35 (arithmetic only, not a measurement).** The 5% interest corner is not
  supported by any source; about 1.3% is the documented order of magnitude. The required
  net ETH funding is linear in `r_b`, so between the 14.5% (no interest) and 9.5% (5%
  interest) endpoints at H = 8% the 1.3% case is about 13.2% a year. That is the
  conservative end, not the favorable one, so the weak prior of I35 gets weaker. The
  verdict stays `needs_measurement`, and I35 keeps the lowest priority of the cheap
  measurements.
- **Still not checked.** Easy Earn eligibility of the owner's account; whether an `LST`
  can be staked from Poland; the settlement fee question (open question 2); I26 inputs.
