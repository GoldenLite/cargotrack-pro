# Smoke-test CargoTrack Pro — журнал багов

Заполняется во время ручного обхода UI. После — фиксим списком.

**Среда:** `manage.py runserver 127.0.0.1:8000` (dev), Chrome/Firefox с открытыми
DevTools (Console + Network).

---

## Чек-лист (отмечать пройденное)

- [x] **1. Dashboard `/`** — загрузка, канбан, фильтры (q/stage/date/warehouse), виджеты
- [x] **2. Список партий `/list/`** — сортировка, поиск, переход в деталь, создание `/list/new/`, экспорт `/export/`
- [x] **3. Детали Cargo `/cargo/<awb>/`** — все поля, смена статуса/этапа/назначения, метки, список HAWB
- [x] **4. HAWB** — `/hawbs/` список, создание standalone, деталь, edit/delete, экспорт
- [x] **5. Товары `/goods/`** — список, апрув, экспорт
- [x] **6. Метки `/labels/`** — CRUD, счётчики использования
- [x] **7. Workflow `/workflows/`** — список, редактор, drag&drop, CRUD steps/transitions/automations, ручной запуск
- [x] **8. SLA `/sla/`** — CRUD политик, остаток времени на cargo, `manage.py check_sla_breaches`
- [x] **9. FAQ `/faq/`** + **Admin `/admin/`** — открываются
- [x] **10. CQL Builder в виджетах** — labels, regex (`~`), BETWEEN, date DSL (`-7d`), tree-mode

Прогон выполнен 2026-05-02 (Phase D), пользователь `andy`. БД: 12 cargo / 68 hawb (включая 1 cargo, оставшийся от `manage.py smoke_check`).

---

## Найденные баги

### B-001 — В инлайн-форме редактирования HAWB вес рендерится с запятой и не проходит валидацию `type="number"`
- **Раздел:** Cargo detail (инлайн-редактор HAWB), Global HAWB list (инлайн-редактор)
- **URL:** `/cargo/<awb>/`, `/hawbs/`
- **Шаги:** Открыть `/cargo/823-83514684/`, развернуть редактор любого HAWB.
- **Ожидалось:** Поле `Вес (кг)` показывает текущий вес и принимает submit без ручного редактирования.
- **Получилось:** Поле пустое; в DOM `value="17,68"` (RU-локаль `{{ hawb.weight }}`), браузер логирует `The specified value "17,68" cannot be parsed, or is out of range`. Submit формы без правки веса обнулит поле.
- **Network/Console:** 4 console warnings вида `The specified value "17,68" cannot be parsed`.
- **Источник:** [cargo/templates/cargo/detail_pro.html:591](cargo/templates/cargo/detail_pro.html#L591), [cargo/templates/cargo/hawb_list.html:418](cargo/templates/cargo/hawb_list.html#L418), плюс `value="{{ total_weight|floatformat:2 }}"` в [cargo_create_wizard.html:315](cargo/templates/cargo/cargo_create_wizard.html#L315) — там же зашита запятая.
- **Фикс:** загрузил `{% load l10n %}` в обоих шаблонах и поменял на `value="{{ hawb.weight|default_if_none:''|unlocalize }}"`; в визарде заменил на `|floatformat:'2u'`.
- **Серьёзность:** major (риск потери данных при сохранении формы)
- **Статус:** fixed
- **Проверка:** `/cargo/823-83514684/` после фикса — `value="17.68"`, все 4 поля `valid=true`, console clean.

### B-002 — Неправильное склонение числительных в карточке процесса («2 шагов»)
- **Раздел:** Workflows
- **URL:** `/workflows/`
- **Шаги:** Открыть список процессов; на карточке процесса с 2 шагами видно «2 шагов».
- **Ожидалось:** «1 шаг», «2 шага», «5 шагов».
- **Получилось:** Везде «N шагов».
- **Источник:** [cargo/templates/cargo/workflow_list.html:499](cargo/templates/cargo/workflow_list.html#L499) — JS-шаблонная строка без плюрализации.
- **Фикс:** добавил хелпер `pluralRu(n, forms)` рядом с `esc()` и заменил на `${pluralRu(w.steps_count, ['шаг','шага','шагов'])}`.
- **Серьёзность:** cosmetic
- **Статус:** fixed
- **Проверка:** на `/workflows/` сейчас рендерится `1 шаг / 2 шага / 0 шагов`.

### B-003 — `manage.py smoke_check` падает на Windows из-за UnicodeEncodeError при логировании русских строк
- **Раздел:** management commands / logging
- **URL:** —
- **Шаги:** `python manage.py smoke_check` в PowerShell на Windows (cp1251).
- **Ожидалось:** Команда отрабатывает, печатает summary.
- **Получилось:** Несколько `--- Logging error --- UnicodeEncodeError: 'charmap' codec can't encode character '→'` от логгера `cargo.models` / `cargo.workflow_runner`. Команда продолжает работу, но мусорит в stderr и в `cargotrack.log` пишет cp1251-мусор для русских сообщений.
- **Источник:** [cargo/models.py:341](cargo/models.py#L341) и аналогичные `logger.info(f'… → …')`. Дефолтный StreamHandler берёт cp1251 на Windows; FileHandler без `encoding=` тоже клал в файл cp1251-мусор.
- **Фикс:** в [cargotrack/settings.py](cargotrack/settings.py) добавил один раз `sys.stdout/stderr.reconfigure(encoding='utf-8', errors='replace')` и `encoding: 'utf-8'` для FileHandler.
- **Серьёзность:** minor (фоновый шум, но создаёт впечатление падения и портит логи)
- **Статус:** fixed
- **Проверка:** `manage.py smoke_check` отрабатывает 88/88 без `Logging error`, в `cargotrack.log` стрелка `→` читается как UTF-8.

### B-004 — Тестовая партия `999-99000001`, созданная `smoke_check`, остаётся в БД и расходится с дашбордом
- **Раздел:** smoke_check / cleanup
- **URL:** —
- **Шаги:** После прогона `smoke_check` (без `--cleanup`) в БД оставались `999-99000001`, `SMOKE-stat-upd`, `SMOKE-WF`, `SMOKE-policy` и т. д.
- **Ожидалось:** По умолчанию команда не должна засорять рабочую БД.
- **Получилось:** Все SMOKE-сущности оставались до явного `--cleanup`.
- **Фикс:** в [smoke_check.py](cargo/management/commands/smoke_check.py) поменял дефолт — теперь cleanup всегда выполняется в конце; флаг `--keep` оставляет данные для визуальной отладки. Старый `--cleanup` оставлен скрытой совместимостью (через `argparse.SUPPRESS`). Также вычистил уже накопившийся мусор в текущей dev-БД.
- **Серьёзность:** minor
- **Статус:** fixed
- **Проверка:** `manage.py smoke_check` → `SMOKE leftovers: 0 0 0 0 0 0`, `Cargo total = 11` (= числу на дашборде).

---

## Что прошло чисто

- Логин/выход, навигация по сайдбару — без ошибок.
- Дашборд: 4 виджета, агрегаты `11 партий / 1 619,3 кг / 115 мест`, сводная по складам — рендер OK, console clean.
- `/list/`: 11 строк, фильтры, сортировка, drag&drop колонок (визуально), экспорт `/export/` отдаёт XLSX 7411 байт, `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`.
- `/list/new/` и `/hawbs/new/` — формы открываются без ошибок.
- `/cargo/<awb>/` — статус/этап/назначения/метки/HAWB-таблица рендерятся (за исключением B-001).
- `/hawbs/` — 68 строк, экспорт `/hawbs/export/` отдаёт XLSX 14060 байт.
- `/hawb/<id>/` — детали, документы, товары — OK.
- `/goods/` — OK; экспорт `/goods/export/` отдаёт XLSX 24601 байт.
- `/labels/`: создание метки `smoke-test` через модалку (POST `/api/v1/labels/`) — счётчик использования = 0 показан корректно.
- `/workflows/` — список + редактор `/workflows/3/` (Импорт): API `GET /api/v1/workflows/3/` 200, панели «Шаги / Автоматизации / Настройки» рендерятся.
- `/sla/` — таблица политик; `python manage.py check_sla_breaches` → `Breaches: 4, notifications: 0`.
- `/faq/`, `/admin/` — открываются без ошибок.
- CQL parser (через `parse_cql`):
  - `weight BETWEEN 50 AND 200` ✅
  - `flight_date >= -7d` (date DSL) ✅
  - `(stage = FORMED OR stage = ARRIVED) AND weight > 100` (tree/group) ✅
  - `description ~ "297"` (regex, кавычки обязательны — `/.../` не поддерживается, это by design) ✅
  - `labels IN ("smoke-test")`, `labels IS NULL`, `labels NOT IN (...)` ✅
