# Round 15 pre-registration — investment and intangibles family on top of P*15, single trial

Written before any round-15 result exists. Round 14 showed that new, theory-signed information helps while
data-driven selection from the existing library does not (rounds 12-13). This round adds one more family from
the same source (SEC XBRL frames API: three new concepts), each with its literature sign, nothing estimated:

  capxg  minus annual growth of capital expenditure (PaymentsToAcquirePropertyPlantAndEquipment)
         (Titman-Wei-Xie 2004: firms that raise investment earn less)
  invg   minus the 12-month change in inventory scaled by total assets (InventoryNet)
         (Thomas-Zhang 2002: inventory build-ups predict lower returns)
  rdm    R&D expense / market cap (ResearchAndDevelopmentExpense)
         (Chan-Lakonishok-Sougiannis 2001: R&D-intensive firms are underpriced); missing -> neutral

Point-in-time: annual values from fiscal-period end + 100 days, balance-sheet values from period end + 75 days.
Cleaning as every other factor.

Single trial: P*18 = the P*15 composite (15 factors) plus these 3, equal weight; everything else frozen
(T9 + D77, Ledoit-Wolf minimum variance). Base = P*15 (round 14).

Decision: proposed to the user only if selection AND holdout Sharpe are both higher than P*15's, the
full-period Sharpe is at least 0.10 higher, and the realistic $100k check confirms it. Otherwise P*15 remains
the research best.
