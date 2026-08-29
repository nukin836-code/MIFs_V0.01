"""Совместимое имя запуска для скрипта сверки канала.

Основная реализация исторически лежит в reconcile_chennel.py. Оставляем
тонкий запускатель, чтобы команда из инструкции и автопроверки использовали
правильное имя без копирования двух версий скрипта.
"""

from __future__ import annotations

import runpy
from pathlib import Path


runpy.run_path(
    str(Path(__file__).with_name("reconcile_chennel.py")),
    run_name="__main__",
)