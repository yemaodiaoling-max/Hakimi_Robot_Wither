"""Fixed identities of this bot deployment (historical defaults; instance config can override self_qq)."""

# The bot's own QQ number on the old NapCat channel; used when config.bot_profile.self_qq is absent.
from hakimi.core.owner import bot_self_id as _bot_self_id, known_bot_ids as _known_bots
SELF_QQ = _bot_self_id()
# Accounts that are bots (ours and other groups' bots); never treated as people to reply to or quote.
KNOWN_BOT_IDS = _known_bots()
