"""Monitoring, diagnostics and auxiliary test functions (phase 3, agent I).

``registers``     controller / follower / AF / EC monitor tables (labels, addresses, reads)
``devicereport``  SOP.Device* usage counters (Device Report mf801)
``logs``          run log files and the log directory viewer (mf124 / mf431)
``measure``       data measurement recorder (mf229)
``customio``      custom IO buttons and dedust (newLang300/301, mf140)
``program``       step programs run through the controller facade
``burnin``        burn-in test (newLang1)
``lighttest``     light test (newLang200)
``interferometer`` laser interferometer program + pitch table (mf180)
``ballarm``       ball-bar pattern (mf702)
``batch``         batch cutting and the production counter (mf705)
``maintain``      maintenance reminders (A251113_0)
``jobwatch``      job start / completion detection
``helpdocs``      offline help built from docs/
Evidence and tags: docs/re/diag.md.
"""
