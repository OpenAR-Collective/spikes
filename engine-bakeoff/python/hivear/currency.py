"""Currencies this installation takes, with the number of minor-unit digits
each has (HIVE-0005). The server states the exponent, so a browser's own
currency data never decides how many minor units an amount is.

A short list on purpose: only currencies whose exponent has been checked.
"""

MINOR_UNIT_DIGITS = {
    "USD": 2,
    "CAD": 2,
    "EUR": 2,
    "GBP": 2,
    "AUD": 2,
    "MXN": 2,
    "JPY": 0,
}
