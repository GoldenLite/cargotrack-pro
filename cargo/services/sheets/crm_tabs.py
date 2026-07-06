"""Единый источник правды для whitelist'a CRM-вкладок специалистов.

Раньше список дублировался в 10 management commands. При расширении
(новый специалист) приходилось менять каждый файл — высокая вероятность
рассинхрона.

Использование:
    from cargo.services.sheets.crm_tabs import SPECIALIST_TABS
"""
from __future__ import annotations


SPECIALIST_TABS: frozenset[str] = frozenset({
    'Беляева Екатерина',
    'Калина Елена',
    'Коробкова Екатерина',
    'Азамов Азам',
    'Никонова Светлана',
    'Подолин Алексей',
    'Пругар Ольга',
    'Алексеева Екатерина',
    'Шушарина Татьяна',
    'Леонова Вера',
    'Лиханова Раиса',
    'Субботина Анна',
})

# Spreadsheet ID — «Рабочее пространство СТО»
CRM_SPREADSHEET_ID: str = '1H7AdXuo_zalnalgrWfVhm0Lau1MdXtFuFbg5pPGfcfI'
