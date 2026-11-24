from decimal import Decimal
from pathlib import Path
import tomllib


def load(path):
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("timezone") != "America/New_York":
        raise ValueError("this version supports America/New_York only")
    for field in ("monthly_usd", "daily_usd"):
        if not Decimal(data[field]).is_finite() or Decimal(data[field]) <= 0:
            raise ValueError("budget must be positive USD")
    if not data.get("account_id") or not data.get("priority_room"):
        raise ValueError("account and room required")
    return data
