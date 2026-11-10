# Venue cost check: Bybit, MEXC, Hyperliquid (v1)

Status: **desk check, 2026-11-10; decides nothing, registers nothing, reads no return.**
The owner asked whether new venues could move the program. The
[2026-11-09 program review](program-review-2026-11-09.md) resumes the program only on a
listed trigger; one of them is "a measurable change in execution costs ... that moves a
stopped line's cost assumption". This note checks that trigger for the three venues an
owner in Poland might trade perpetuals on, at the standard tariff. Sources: the venues'
official fee pages and API docs, their public instrument endpoints (static contract
parameters, read 2026-11-10 around 14:00 UTC) and the repository's own records. Where
access or a tariff is not confirmed, the table says `unknown`. The note uses no market
price; see [Blind window](#blind-window) for the three that were looked at.

## Result

| Question                                   | Bybit                                                                                                                                       | MEXC                                                                                                                                                                     | Hyperliquid                                                                                                         |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| Perpetuals through the trading API, Poland | confirmed: the owner opened a USDT perpetual position on 2026-10-07 (account believed global; Bybit EU's API covers only Bybit EU products) | `unknown`: the owner says the account is open; no trade recorded                                                                                                         | `unknown`: Poland is not on third-party lists of restricted places; the official terms were not read; no EU licence |
| Published taker / maker                    | 0.055% / 0.02% (VIP 0, official schedule)                                                                                                   | 0.04% / 0.01% is the contract default on 982 of 1,100 USDT perpetuals; 105 are at 0 / 0 (promotion); the fee page says rates can vary by region, so `unknown` for Poland | 0.045% / 0.015% (base tier, official docs)                                                                          |
| Taker entry and exit, published fees only  | 11 bps                                                                                                                                      | 8 bps (region not confirmed)                                                                                                                                             | 9 bps                                                                                                               |
| Difference of published fees against Bybit | -                                                                                                                                           | about 3 bps                                                                                                                                                              | about 2 bps                                                                                                         |

The fee rows are published rates. The saving an actual account gets is not confirmed:
it needs the account's own fee rate (Bybit shows it only after verification; MEXC by
region).

### Order size rules

Static contract parameters, with the rule for whether an order of notional N at price P
opens:

| Venue       | Parameters (BTC, SOL, DOGE)                                                                                            | Opening rule                                                                                                                                                           |
| ----------- | ---------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bybit       | `qtyStep` and `minOrderQty`: 0.001, 0.1, 1; `minNotionalValue` 5 USDT for all three                                    | quantity = max(`minOrderQty`, 5 / P) rounded **up** to `qtyStep`; the order is that quantity times P. For BTC, a USD 50 order fits only while P is at most USD 50,000. |
| MEXC        | contract size: 0.0001 BTC, 0.1 SOL, 100 DOGE; `minVol` 1 contract, `volUnit` 1                                         | quantity is a whole number of contracts, at least one: the smallest order is one contract times P, and sizing moves in steps of one contract.                          |
| Hyperliquid | `szDecimals`: 5, 2, 0 (size steps 0.00001 BTC, 0.01 SOL, 1 DOGE); minimum order value USD 10 for perpetuals (API docs) | notional at least USD 10, size rounded to `szDecimals`.                                                                                                                |

So at USD 50, BTC on Bybit is the one combination that can fail on the minimum alone,
and on Bybit and MEXC the step of a mid-priced alt (0.1 SOL, one MEXC contract) is a
sizable part of the order: sizing is coarse.

## Which design a lower fee could move

The 30 bps round trip is the cost assumption of HYP-031's minute protocol (also used by
HYP-032 and HYP-033): 5.5 bps of Bybit taker fee and 9.5 bps of spread and slippage per
side. A 2 to 3 bps difference in published fees would make it 27 to 28 bps. Taking all
11 bps of fees out leaves 19 bps, but that is only the arithmetic remainder of the
assumption: spread and slippage were never measured separately.

| Line                                                                              | Why a lower fee does not change it                                                                                                                                                                                                                                                                                                                            |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| HYP-031 post-funding, minute ([record](bybit-post-funding-minute-v1.md))          | Mean gross was +17.0 bps; the size bar is +20 bps net at 30 bps costs, so a pass needs about +50 bps gross. It would fail even at zero cost. The excess over matched controls (+19.1 bps against a +30 bps bar) does not depend on fees. A different venue would also change the event sample, so Bybit's result cannot be carried over by subtracting a fee. |
| HYP-015 hold12h                                                                   | Mean net -0.64% (-64 bps) after costs and funding; 2 to 3 bps does not reach it.                                                                                                                                                                                                                                                                              |
| HYP-032 to HYP-036, family 5, BTC trend                                           | Stopped on power, coverage or control balance; costs were not the cause.                                                                                                                                                                                                                                                                                      |
| Execution edge, limit against taker ([inventory](execution-edge-inventory-v1.md)) | Powered, but no signal on record pays for a cheaper entry.                                                                                                                                                                                                                                                                                                    |
| HYP-012 v2                                                                        | Registered with Bybit as the venue; changing the venue changes the estimand. It continues under its own rules.                                                                                                                                                                                                                                                |

**Reading.** At published standard tariffs no venue gives grounds for the cost trigger:
the difference is 2 to 3 bps a round trip, and no stopped design is within tens of bps
of its bar on costs. The check closes here. Spread and depth are worth measuring only
for a named candidate and an allowed data window. Hyperliquid's address-level trade
feed stays in the queue until there is a question those addresses would answer.

## Blind window

The HYP-012 v2 blind window ([strategy v2](research-strategy-v2.md)) closes every market
datum from 2026-09-29 on. While preparing this note, on 2026-11-10 around 14:00 UTC,
Bybit's `/v5/market/tickers` was called for BTCUSDT, SOLUSDT and DOGEUSDT, and the last
price of each was read to express order steps in USD. Nothing else in those responses
was printed, the saved responses were deleted unread, and the prices are not used
here. The exposure is recorded in the
[seen windows registry](seen-windows-registry.md).

## Sources

- Bybit trading fee structure: <https://www.bybit.com/en/help-center/article/trading-fee-structure>
- Bybit futures trading rules (minimum order value): <https://www.bybit.com/en/help-center/article/Futures-Trading-Rules?category=cd60af6303161fd598>
- Bybit EU API scope: <https://www.bybit.eu/en-EU/help-center/article/How-to-retrieve-API-documentations>
- MEXC fee page (regional note): <https://www.mexc.com/fee/>
- Hyperliquid fees: <https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees>
- Hyperliquid API error responses (USD 10 minimum): <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses>
- Hyperliquid restricted places (third party, not the official terms): <https://www.datawallet.com/crypto/hyperliquid-supported-and-restricted-countries>
- Public endpoints for static parameters: Bybit `/v5/market/instruments-info`
  (category `linear`), MEXC `/api/v1/contract/detail`, Hyperliquid `info` with
  `{"type": "meta"}`.
