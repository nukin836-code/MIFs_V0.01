"""Спрощена заглушка rapidfuzz.fuzz.token_set_ratio (difflib). Однакова для
старої й нової версій, тому порівняння результатів пошуку коректне."""
from difflib import SequenceMatcher
class fuzz:
    @staticmethod
    def token_set_ratio(a, b):
        sa, sb = " ".join(sorted(set(a.split()))), " ".join(sorted(set(b.split())))
        return 100.0 * SequenceMatcher(None, sa, sb).ratio()
