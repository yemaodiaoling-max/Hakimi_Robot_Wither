"""Translate legacy Flash preferences when selecting a Vertex model."""
HYDROGEN = 'gemini-4-hydrogen'
LEGACY_FLASH = frozenset({'gemini-3.8-flash', 'gemini-3.5-flash-lite'})


def vertex_model(model):
    return HYDROGEN if model in LEGACY_FLASH else model
