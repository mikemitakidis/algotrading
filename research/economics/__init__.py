"""research.economics — realistic execution economics (canonical B10; M21 WP12).

* fees      dated IBKR commission plans and US regulatory fees (sourced assumptions)
* model     point-in-time liquidity, spread / impact / slippage, participation
            caps, halts and zero volume, short borrow, splits and dividends,
            and the itemised price of one round trip
* analysis  size sensitivity, edge after costs, realised-vs-modelled fee
            calibration, and re-pricing of lane A labels

Offline research only: nothing here contacts a broker or market-data service.
Every coefficient is an assumption to calibrate against realised paper fills.
"""
