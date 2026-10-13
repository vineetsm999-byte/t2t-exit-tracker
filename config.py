# ============================================================
# config.py  -  settings you may want to change
# ============================================================

LOOKBACK_DAYS = 45          # how many days back to look on every run

# A circular is picked up if its SUBJECT contains any of these (not case sensitive)
NSE_SUBJECT_KEYWORDS = ["trade for trade"]
BSE_SUBJECT_KEYWORDS = ["change in group of equity shares"]

# The circular TEXT must contain one of these phrases, so that we only keep
# stocks that are LEAVING T2T (not entering it).
NSE_EXIT_PHRASES = ["rolling segment"]

# Your lock-in sheet is used to fill the Listing Date (matched by company name)
LOCKIN_CSV_URL = ("https://raw.githubusercontent.com/vineetsm999-byte/"
                  "ipo---lockin/main/lockin_data.csv")

# BSE: how many past days of notices to check on each run
BSE_SCAN_DAYS = 10

# BSE: notices are read as PDFs (no browser). Set False to switch BSE off.
BSE_ENABLED = True
