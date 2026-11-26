"""Integer units used by the drawing and search ledgers."""
from decimal import Decimal, ROUND_CEILING

NANO = 1_000_000_000


def nano(value):
    """Round a nonnegative USD amount up to the next billionth of a dollar."""
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid_search_budget")  # historical error code
    return int((amount * NANO).to_integral_value(rounding=ROUND_CEILING))
