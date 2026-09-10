"""Universe construction for the 5-minute ORB replication.

The paper trades ~7,000 US stocks including delisted ones (CRSP). Alpaca's asset
master is *today's* tradable list, so anything acquired, merged, or bankrupted is
simply absent — the survivorship bias is structural and cannot be fixed from this
data source. See momentum_orb.md.

The candidate list is seeded from the tickers the paper itself names in Tables 4
and 5 — the 25 best AND 25 worst performers across all four time frames — plus
liquid large caps. Seeding from both tails matters: a universe built only from
the paper's winners would bake the result in before a single bar is fetched.
"""

from __future__ import annotations

# Table 4 — best performers, all four time frames.
PAPER_BEST = """
DDD FSLR NVDA SWBI RCL W VIR EXAS ALK FOSL WW OKTA PBF AMD TSLA ADBE ACAD ELV
TWLO TDOC SPLK PARA WDC NWL SQ
CAR LITE MTCH ASML OMF BWA NFLX NKTR CDNS LRCX TER FLR CSGP LIN QQQ BCRX YELP
AZTA WOLF
MXIM SAVE HRB NET GDDY NTNX MA TMX OLN AAOI TT
THC TKAT DISH EXEL BA IONS VLO INTU FIS KA TRU STZ AXDX BAX VRTX MAR
"""

# Table 5 — worst performers, all four time frames.
PAPER_WORST = """
CMC TRGP CSX CNP BJ PSTG WMB TT HP ALLY FL PSX WYNN DOW URBN APC ROST JBL DD
MARA VOYA BLMN BRO HOG SKX
CLR TSCO IVZ INCY YUMC TFFP GM REG FCX MET EQT KNX EXPE BG ROK MU IOVA MGY SEDG
TSN TPR RES
BIIB AEO KMX VST CNK HAL EA XYL ANF MCK GPS NKE DAL INTC XEC SM PHM SGEN
DINO DBI ADI BKR GILD KDNY DBX UBX CMCSA EBAY LBRT EVLO OGE NTRS EWBC MRO CTSH
TMUS
"""

# Liquid large caps that dominate US share volume and are natural day-trade
# candidates. Not from the paper — added so the universe is not composed purely
# of names selected on 2016-2023 outcomes.
LARGE_CAPS = """
AAPL MSFT AMZN GOOGL META NFLX JPM BAC WFC C GS MS V PXD XOM CVX COP SLB
PFE MRK ABBV JNJ UNH LLY BMY AMGN T VZ DIS CMCSA KO PEP PG WMT HD LOW TGT COST
CRM ORCL IBM CSCO QCOM AVGO TXN MU AMAT INTC AMD NVDA MRVL ON
F GM RIVN LCID NIO PLTR SOFI COIN HOOD RBLX SNAP PINS UBER LYFT ABNB DASH
SPY QQQ IWM XLF XLE
"""


def candidates() -> list[str]:
    """Deduplicated candidate tickers, sorted."""
    raw = (PAPER_BEST + PAPER_WORST + LARGE_CAPS).split()
    return sorted({t.strip().upper() for t in raw if t.strip()})


if __name__ == "__main__":
    c = candidates()
    print(f"{len(c)} candidates")
    print(" ".join(c))
