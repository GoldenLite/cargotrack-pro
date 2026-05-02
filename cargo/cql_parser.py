"""
Cargo Query Language (CQL) — парсер для фильтрации партий (Cargo) и накладных (HAWB).

Архитектура:
  Лексер (_tokenize)  →  список токенов
  Парсер (_Parser)    →  AST (JSON-совместимый dict)        ← parse_to_ast()
  Компилятор (_compile) →  Django Q-объект                  ← compile_ast()
  parse_cql()         =  compile_ast(parse_to_ast(...))     (обратная совместимость)

Грамматика:
  expr      = or_expr
  or_expr   = and_expr  ( 'OR'  and_expr )*
  and_expr  = unary     ( 'AND' unary    )*
  unary     = 'NOT' atom | atom
  atom      = '(' expr ')' | condition

  condition =
      FIELD simple_op VALUE
    | FIELD 'IN'     '(' value_list ')'
    | FIELD 'NOT IN' '(' value_list ')'
    | FIELD 'IS' 'NULL'
    | FIELD 'IS' 'NOT' 'NULL'
    | FIELD 'CONTAINS' VALUE
    | FIELD 'BETWEEN' VALUE 'AND' VALUE

  simple_op  = '=' | '!=' | '<>' | '>' | '>=' | '<' | '<=' | '~' | '!~'
  value_list = VALUE (',' VALUE)*
  VALUE      = quoted_string | number | date | identifier | TRUE | FALSE | NULL | ME | DATE_DSL

  DATE_DSL   = now() | today() | startOfDay() | endOfDay()
             | [+-]\\d+[dwMhy]   (relative offset, например -7d, +2w, -3M)

AST-формат:
  group:     {type:'group', op:'AND'|'OR', negated:bool, children:[node...]}
  condition: {type:'condition', field:str, op:str, value:scalar|list|null, negated:bool}

Примеры:
  stage = ARRIVED AND weight > 100
  (stage = CUSTOMS OR stage = ARRIVED) AND is_problematic = true
  assigned_to = me AND days_in_warehouse > 7
  stage IN (ARRIVED, CUSTOMS) AND departure_iata = "AMS"
  weight BETWEEN 100 AND 500
  description ~ "(?i)экспорт"
  flight_date >= -7d
  labels IS NULL OR labels NOT IN ("СТО_подано", "СТО_выпуск")
"""
import re
import datetime
from django.db.models import F, Q
from django.utils import timezone

# ── Описание полей ────────────────────────────────────────────────────────────
# Формат поля (унифицированный dict):
#   {
#     'db_field': 'orm__path' | None,    # None для специальных вычисляемых полей
#     'type':     'str'|'num'|'date'|'datetime'|'bool'|'special',
#     'm2m':      bool                   # True → M2M-поле (метки), требует distinct()
#   }
#
# Для краткости поддерживается также «короткий» кортежный формат (db_field, type) —
# он автоматически нормализуется в dict в _normalize_fields().

CARGO_FIELDS = {
    # Основные
    'stage':               ('stage',                       'str'),
    'weight':              ('weight',                      'num'),
    'pieces_declared':     ('pieces_declared',             'num'),
    'pieces':              ('pieces_declared',             'num'),  # alias
    'flight_date':         ('flight_date',                 'date'),
    'departure_date':      ('departure_date',              'date'),
    'shp_type':            ('shp_type',                    'str'),
    'cpc_code':            ('cpc_code',                    'str'),
    # Склад
    'warehouse':           ('warehouse_license',           'str'),
    'warehouse_name':      ('warehouse_name',              'str'),
    # Идентификация
    'awb':                 ('awb_number',                  'str'),
    'description':         ('description',                 'str'),
    'customs_declaration': ('customs_declaration_number',  'str'),
    # Маршрут
    'departure_iata':      ('departure_iata',              'str'),
    'arrival_iata':        ('arrival_iata',                'str'),
    'transport_mode':      ('transportation_mode',         'num'),
    # Финансы
    'invoice_value':       ('invoice_value',               'num'),
    # Участники
    'assigned_to':         ('assignments__user__username', 'str'),
    # Флаги
    'is_draft':            ('is_draft',                    'bool'),
    'is_self_clearance':   ('is_self_clearance',           'bool'),
    'is_transit':          ('is_transit',                  'bool'),
    # Вычисляемые (специальная обработка)
    'is_problematic':      (None,                          'special'),
    'days_in_warehouse':   (None,                          'special'),
    # Обратный поиск по HAWB (накладным внутри партии)
    'hawb_number':         ('hawbs__hawb_number',          'str'),
    'hawb_consignee':      ('hawbs__consignee_name',       'str'),
    'hawb_status':         ('hawbs__logistics_status',     'str'),
    'hawb_cargo_type':     ('hawbs__cargo_type',           'str'),
    # Метки (M2M)
    'labels':              {'db_field': 'labels__name', 'type': 'str', 'm2m': True},
}

HAWB_FIELDS = {
    # Идентификация
    'hawb_number':         ('hawb_number',                 'str'),
    'description':         ('description',                 'str'),
    # Типы
    'cargo_type':          ('cargo_type',                  'str'),
    'shipment_type':       ('shipment_type',               'str'),
    # Получатель
    'consignee':           ('consignee_name',              'str'),
    'consignee_city':      ('consignee_city',              'str'),
    'consignee_inn':       ('consignee_inn',               'str'),
    # Статусы
    'logistics_status':    ('logistics_status',            'str'),
    'customs_status':      ('customs_status',              'str'),
    # Параметры
    'weight':              ('weight',                      'num'),
    'pieces_declared':     ('pieces_declared',             'num'),
    'pieces':              ('pieces_declared',             'num'),  # alias
    'invoice_value':       ('invoice_value',               'num'),
    # Таможня
    'customs_declaration': ('customs_declaration_number',  'str'),
    'release_date':        ('release_date',                'date'),
    # Участники
    'assigned_to':         ('assigned_to__username',       'str'),
    # Связь с партией
    'mawb':                ('mawb__awb_number',            'str'),
    'is_standalone':       (None,                          'special'),  # mawb IS NULL
    # Склад (берётся из связанной партии)
    'warehouse':           ('mawb__warehouse_license',     'str'),
    'warehouse_name':      ('mawb__warehouse_name',        'str'),
    'days_in_warehouse':   (None,                          'special'),  # от scan_into_bond
    # Метки (M2M)
    'labels':              {'db_field': 'labels__name', 'type': 'str', 'm2m': True},
}

# Алиас для обратной совместимости
FIELDS = CARGO_FIELDS


def _normalize_fields(fields: dict) -> dict:
    """Приводит CARGO_FIELDS/HAWB_FIELDS к единому dict-формату.
    Кортеж (db_field, type) → {'db_field':…, 'type':…, 'm2m':False}."""
    result = {}
    for name, spec in fields.items():
        if isinstance(spec, dict):
            result[name] = {
                'db_field': spec.get('db_field'),
                'type':     spec.get('type', 'str'),
                'm2m':      bool(spec.get('m2m', False)),
            }
        else:
            db_field, ftype = spec
            result[name] = {'db_field': db_field, 'type': ftype, 'm2m': False}
    return result


# ── Лексер ────────────────────────────────────────────────────────────────────
# DATE_DSL должен идти РАНЬШЕ NUMBER (иначе -7d распарсится как минус-число).
_PATTERNS = [
    ('LPAREN',   r'\('),
    ('RPAREN',   r'\)'),
    ('COMMA',    r','),
    ('OP',       r'!=|<>|>=|<=|>|<|=|!~|~'),
    ('DATE_DSL', r'(?:now|today|startOfDay|endOfDay)\s*\(\s*\)|[+-]\d+[dwMhy]'),
    ('DATE',     r'\d{4}-\d{2}-\d{2}'),
    ('NUMBER',   r'\d+(?:\.\d+)?'),
    ('STRING',   r'"[^"]*"|\'[^\']*\''),
    ('KEYWORD',  r'\b(?:AND|OR|NOT|IS|IN|CONTAINS|BETWEEN|NULL|TRUE|FALSE|ME)\b'),
    ('IDENT',    r'[A-Za-z_А-Яа-яЁё][A-Za-z0-9_А-Яа-яЁё]*'),
    ('SKIP',     r'\s+'),
]
# Регистр для KEYWORD/функций — нечувствительный, для DATE_DSL частично чувствительный
# (startOfDay/endOfDay), но re.IGNORECASE приемлемо.
_MASTER = re.compile(
    '|'.join(f'(?P<{n}>{p})' for n, p in _PATTERNS),
    re.IGNORECASE,
)


def _tokenize(text: str) -> list:
    tokens = []
    pos = 0
    for m in _MASTER.finditer(text):
        if m.start() != pos:
            bad = text[pos:m.start()].strip()
            raise CQLError(f'Неожиданный символ возле: {bad!r}')
        pos = m.end()
        kind = m.lastgroup
        val = m.group()
        if kind == 'SKIP':
            continue
        if kind == 'KEYWORD':
            tokens.append((val.upper(), val.upper()))
        elif kind == 'STRING':
            tokens.append(('STRING', val[1:-1]))
        elif kind == 'DATE_DSL':
            # нормализуем функции к нижнему регистру с (), относительные оставляем как есть
            v = val.lower().replace(' ', '')
            tokens.append(('DATE_DSL', v))
        else:
            tokens.append((kind, val))
    if pos != len(text):
        bad = text[pos:].strip()
        raise CQLError(f'Неожиданный символ возле: {bad!r}')
    tokens.append(('EOF', ''))
    return tokens


# ── Исключения и AST-узлы ─────────────────────────────────────────────────────
class CQLError(Exception):
    pass


def _group(op: str, children: list, negated: bool = False) -> dict:
    return {'type': 'group', 'op': op, 'negated': negated, 'children': children}


def _cond(field: str, op: str, value, negated: bool = False) -> dict:
    return {'type': 'condition', 'field': field, 'op': op,
            'value': value, 'negated': negated}


# ── Парсер: токены → AST ──────────────────────────────────────────────────────
class _Parser:
    """Возвращает AST в JSON-совместимом формате (dict)."""
    def __init__(self, tokens: list, fields: dict):
        self.tokens = tokens
        self.pos = 0
        self.fields = fields

    def _peek(self):
        return self.tokens[self.pos]

    def _consume(self, expected=None):
        tok = self.tokens[self.pos]
        if expected and tok[0] != expected:
            raise CQLError(f'Ожидался {expected}, получен {tok[0]!r} ({tok[1]!r})')
        self.pos += 1
        return tok

    def _match(self, *kinds):
        return self.tokens[self.pos][0] in kinds

    # ─ грамматика ─
    def parse(self) -> dict:
        node = self._or_expr()
        if not self._match('EOF'):
            raise CQLError(f'Неожиданный токен: {self._peek()[1]!r}')
        # Корень всегда group, чтобы builder UI имел стабильную точку входа.
        if node['type'] != 'group':
            node = _group('AND', [node])
        return node

    def _or_expr(self) -> dict:
        left = self._and_expr()
        if not self._match('OR'):
            return left
        children = [left]
        while self._match('OR'):
            self._consume()
            children.append(self._and_expr())
        return _group('OR', children)

    def _and_expr(self) -> dict:
        left = self._unary()
        if not self._match('AND'):
            return left
        children = [left]
        while self._match('AND'):
            self._consume()
            children.append(self._unary())
        return _group('AND', children)

    def _unary(self) -> dict:
        if self._match('NOT'):
            self._consume()
            inner = self._atom()
            if inner['type'] == 'group':
                inner['negated'] = not inner.get('negated', False)
                return inner
            # condition: оборачиваем в группу с NOT, чтобы AST оставался единообразным
            return _group('AND', [inner], negated=True)
        return self._atom()

    def _atom(self) -> dict:
        if self._match('LPAREN'):
            self._consume()
            node = self._or_expr()
            self._consume('RPAREN')
            if node['type'] != 'group':
                node = _group('AND', [node])
            return node
        return self._condition()

    def _condition(self) -> dict:
        field_tok = self._consume('IDENT')
        field_name = field_tok[1].lower()
        if field_name not in self.fields:
            known = ', '.join(sorted(self.fields))
            raise CQLError(f'Неизвестное поле: {field_name!r}. Доступные поля: {known}')

        # IS NULL / IS NOT NULL
        if self._match('IS'):
            self._consume()
            negate = False
            if self._match('NOT'):
                self._consume()
                negate = True
            self._consume('NULL')
            return _cond(field_name, 'IS NOT NULL' if negate else 'IS NULL', None)

        # NOT IN / NOT BETWEEN — поддерживаем оба
        if self._match('NOT') and self.tokens[self.pos + 1][0] in ('IN', 'BETWEEN'):
            self._consume()  # NOT
            following = self._peek()[0]
            if following == 'IN':
                self._consume()
                values = self._value_list()
                return _cond(field_name, 'NOT IN', values)
            else:  # BETWEEN
                self._consume()
                lo = self._raw_value()
                self._consume('AND')
                hi = self._raw_value()
                return _cond(field_name, 'NOT BETWEEN', [lo, hi])

        # IN
        if self._match('IN'):
            self._consume()
            values = self._value_list()
            return _cond(field_name, 'IN', values)

        # BETWEEN
        if self._match('BETWEEN'):
            self._consume()
            lo = self._raw_value()
            self._consume('AND')
            hi = self._raw_value()
            return _cond(field_name, 'BETWEEN', [lo, hi])

        # CONTAINS
        if self._match('CONTAINS'):
            self._consume()
            return _cond(field_name, 'CONTAINS', self._raw_value())

        # Простой оператор (включая ~ / !~)
        op_tok = self._consume('OP')
        op = op_tok[1].replace('<>', '!=')
        return _cond(field_name, op, self._raw_value())

    def _value_list(self) -> list:
        self._consume('LPAREN')
        values = [self._raw_value()]
        while self._match('COMMA'):
            self._consume()
            values.append(self._raw_value())
        self._consume('RPAREN')
        return values

    def _raw_value(self):
        kind, val = self._peek()
        if kind in ('STRING', 'DATE', 'NUMBER', 'IDENT', 'DATE_DSL'):
            self._consume()
            return val
        if kind in ('TRUE', 'FALSE', 'NULL', 'ME'):
            self._consume()
            if kind == 'ME':
                return '__ME__'  # маркер, разрешается в _resolve_value
            if kind == 'NULL':
                return None
            return kind  # 'TRUE' / 'FALSE'
        raise CQLError(f'Ожидалось значение, получен {kind!r} ({val!r})')


# ── Компилятор: AST → Q ───────────────────────────────────────────────────────
_DATE_DSL_RE = re.compile(r'^([+-])(\d+)([dwMhy])$')


def _resolve_date_dsl(val: str):
    """now() → datetime now (aware), today() → date today, -7d → date 7 дней назад,
    +2w → date через 2 недели, -3M → 3 месяца назад (≈30 дней/мес), -1y → год назад."""
    v = val.strip()
    low = v.lower().replace(' ', '')
    if low == 'now()':
        return timezone.now()
    if low == 'today()':
        return timezone.localdate()
    if low == 'startofday()':
        d = timezone.localdate()
        return datetime.datetime.combine(d, datetime.time.min, tzinfo=timezone.get_current_timezone())
    if low == 'endofday()':
        d = timezone.localdate()
        return datetime.datetime.combine(d, datetime.time.max, tzinfo=timezone.get_current_timezone())
    m = _DATE_DSL_RE.match(v)
    if not m:
        raise CQLError(f'Не распознан date-DSL: {val!r}')
    sign, num, unit = m.group(1), int(m.group(2)), m.group(3)
    delta_days = {'d': num, 'w': num * 7, 'M': num * 30, 'y': num * 365, 'h': 0}[unit]
    base = timezone.localdate()
    delta = datetime.timedelta(days=delta_days)
    if unit == 'h':
        return timezone.now() + (datetime.timedelta(hours=num) if sign == '+' else -datetime.timedelta(hours=num))
    return base + delta if sign == '+' else base - delta


def _is_date_dsl(val) -> bool:
    if not isinstance(val, str):
        return False
    low = val.lower().replace(' ', '')
    if low in ('now()', 'today()', 'startofday()', 'endofday()'):
        return True
    return bool(_DATE_DSL_RE.match(val))


def _resolve_value(raw, ftype: str, context: dict):
    """Превращает «сырое» значение из AST в Python-значение для ORM.
    Понимает маркер __ME__ и date-DSL."""
    if raw == '__ME__':
        return context.get('me', '')
    if raw is None:
        return None
    if ftype in ('date', 'datetime') and _is_date_dsl(raw):
        return _resolve_date_dsl(raw)
    if ftype == 'num':
        try:
            return float(raw) if '.' in str(raw) else int(raw)
        except (ValueError, TypeError):
            raise CQLError(f'Ожидалось число, получено {raw!r}')
    if ftype == 'date':
        try:
            return datetime.date.fromisoformat(str(raw))
        except ValueError:
            raise CQLError(f'Ожидалась дата (ГГГГ-ММ-ДД), получено {raw!r}')
    if ftype == 'bool':
        if isinstance(raw, str) and raw.upper() == 'FALSE':
            return False
        return str(raw).lower() in ('true', '1', 'yes', 'да')
    return str(raw)


def _compile_special(field_name: str, op: str, raw, fields: dict, context: dict) -> Q:
    """Логика для is_standalone, is_problematic, days_in_warehouse."""
    if field_name == 'is_standalone':
        bool_val = _resolve_value(raw, 'bool', context)
        if op == '!=':
            bool_val = not bool_val
        elif op != '=':
            raise CQLError(f'Оператор {op!r} не поддерживается для is_standalone')
        q = Q(mawb__isnull=True)
        return q if bool_val else ~q

    if field_name == 'is_problematic':
        bool_val = _resolve_value(raw, 'bool', context)
        if op == '!=':
            bool_val = not bool_val
        elif op != '=':
            raise CQLError(f'Оператор {op!r} не поддерживается для is_problematic')
        # «Проблемная» = продолжительность хранения > 7 дней.
        # Хранение считается до scan_out_of_bond (если выпущена) либо до now().
        seven_d = datetime.timedelta(days=7)
        released_long = (
            Q(scan_into_bond__isnull=False)
            & Q(scan_out_of_bond__isnull=False)
            & Q(scan_out_of_bond__gt=F('scan_into_bond') + seven_d)
        )
        open_long = (
            Q(scan_into_bond__isnull=False)
            & Q(scan_out_of_bond__isnull=True)
            & Q(scan_into_bond__lt=timezone.now() - seven_d)
        )
        q = released_long | open_long
        return q if bool_val else ~q

    if field_name == 'days_in_warehouse':
        try:
            n = int(float(raw))
        except (ValueError, TypeError):
            raise CQLError(f'days_in_warehouse требует число, получено {raw!r}')
        # Сравнение с N дней. Хранение = (scan_out_of_bond OR now()) - scan_into_bond.
        delta_n        = datetime.timedelta(days=n)
        delta_n_minus  = datetime.timedelta(days=n - 1)
        ref            = timezone.now() - delta_n           # "давнее" границы для открытых
        ref_minus      = timezone.now() - delta_n_minus
        base = Q(scan_into_bond__isnull=False)
        # Закрытые (выпущенные)
        out_eq  = Q(scan_out_of_bond__isnull=False) & Q(scan_out_of_bond__gte=F('scan_into_bond') + delta_n) & Q(scan_out_of_bond__lt=F('scan_into_bond') + delta_n + datetime.timedelta(days=1))
        out_gt  = Q(scan_out_of_bond__isnull=False) & Q(scan_out_of_bond__gt=F('scan_into_bond') + delta_n)
        out_gte = Q(scan_out_of_bond__isnull=False) & Q(scan_out_of_bond__gte=F('scan_into_bond') + delta_n)
        out_lt  = Q(scan_out_of_bond__isnull=False) & Q(scan_out_of_bond__lt=F('scan_into_bond') + delta_n)
        out_lte = Q(scan_out_of_bond__isnull=False) & Q(scan_out_of_bond__lte=F('scan_into_bond') + delta_n)
        # Открытые (ещё на СВХ): now() − scan_in vs n
        open_   = Q(scan_out_of_bond__isnull=True)
        open_eq  = open_ & Q(scan_into_bond__gt=ref - datetime.timedelta(days=1)) & Q(scan_into_bond__lte=ref)
        open_gt  = open_ & Q(scan_into_bond__lt=ref)
        open_gte = open_ & Q(scan_into_bond__lte=ref)
        open_lt  = open_ & Q(scan_into_bond__gt=ref)
        open_lte = open_ & Q(scan_into_bond__gte=ref)
        if op == '=':   return base & (out_eq  | open_eq)
        if op == '>':   return base & (out_gt  | open_gt)
        if op == '>=':  return base & (out_gte | open_gte)
        if op == '<':   return base & (out_lt  | open_lt)
        if op == '<=':  return base & (out_lte | open_lte)
        if op == '!=':  return ~(base & (out_eq | open_eq))
        raise CQLError(f'Оператор {op!r} не поддерживается для days_in_warehouse')

    raise CQLError(f'Спец-поле {field_name!r} не имеет обработчика для {op!r}')


def _compile_condition(node: dict, fields: dict, context: dict) -> Q:
    field_name = node['field']
    op = node['op']
    raw = node['value']

    if field_name not in fields:
        known = ', '.join(sorted(fields))
        raise CQLError(f'Неизвестное поле: {field_name!r}. Доступные поля: {known}')

    spec = fields[field_name]
    db_field, ftype, m2m = spec['db_field'], spec['type'], spec['m2m']

    # IS NULL / IS NOT NULL
    if op in ('IS NULL', 'IS NOT NULL'):
        if db_field is None:
            raise CQLError(f'IS NULL не поддерживается для поля {field_name!r}')
        if m2m:
            # Для M2M: __isnull=True означает "нет ни одной связи"
            q = Q(**{f'{db_field.split("__", 1)[0]}__isnull': True})
        elif ftype == 'str':
            # CharField с blank=True хранит "нет значения" как '' а не NULL,
            # поэтому в IS NULL должны попадать оба варианта.
            q = Q(**{f'{db_field}__isnull': True}) | Q(**{db_field: ''})
        else:
            q = Q(**{f'{db_field}__isnull': True})
        return ~q if op == 'IS NOT NULL' else q

    # IN / NOT IN
    if op in ('IN', 'NOT IN'):
        if db_field is None:
            raise CQLError(f'{op} не поддерживается для поля {field_name!r}')
        values = [_resolve_value(v, ftype, context) for v in raw]
        suffix = '__in'
        if ftype == 'str' and not m2m:
            # Case-insensitive in for strings — собираем OR из __iexact
            # но для производительности оставим __in (БД обычно case-insensitive в collation)
            pass
        q = Q(**{f'{db_field}{suffix}': values})
        return ~q if op == 'NOT IN' else q

    # BETWEEN / NOT BETWEEN
    if op in ('BETWEEN', 'NOT BETWEEN'):
        if db_field is None or ftype == 'special':
            raise CQLError(f'BETWEEN не поддерживается для поля {field_name!r}')
        lo = _resolve_value(raw[0], ftype, context)
        hi = _resolve_value(raw[1], ftype, context)
        q = Q(**{f'{db_field}__gte': lo}) & Q(**{f'{db_field}__lte': hi})
        return ~q if op == 'NOT BETWEEN' else q

    # CONTAINS
    if op == 'CONTAINS':
        if db_field is None:
            raise CQLError(f'CONTAINS не поддерживается для поля {field_name!r}')
        return Q(**{f'{db_field}__icontains': _resolve_value(raw, 'str', context)})

    # ~ / !~  (regex)
    if op in ('~', '!~'):
        if db_field is None:
            raise CQLError(f'{op} не поддерживается для поля {field_name!r}')
        pattern = str(_resolve_value(raw, 'str', context))
        if len(pattern) > 200:
            raise CQLError(f'Слишком длинный regex (> 200 символов)')
        try:
            re.compile(pattern)
        except re.error as e:
            raise CQLError(f'Некорректный regex {pattern!r}: {e}')
        q = Q(**{f'{db_field}__regex': pattern})
        return ~q if op == '!~' else q

    # Спец-поля
    if ftype == 'special':
        return _compile_special(field_name, op, raw, fields, context)

    # Простые операторы =, !=, >, >=, <, <=
    coerced = _resolve_value(raw, ftype, context)
    if op == '=':
        suffix = '__iexact' if ftype == 'str' else ''
        return Q(**{f'{db_field}{suffix}': coerced})
    if op == '!=':
        suffix = '__iexact' if ftype == 'str' else ''
        return ~Q(**{f'{db_field}{suffix}': coerced})
    op_suffix = {'>': '__gt', '>=': '__gte', '<': '__lt', '<=': '__lte'}
    if op not in op_suffix:
        raise CQLError(f'Неизвестный оператор: {op!r}')
    return Q(**{f'{db_field}{op_suffix[op]}': coerced})


def _compile_node(node: dict, fields: dict, context: dict) -> Q:
    if node['type'] == 'condition':
        q = _compile_condition(node, fields, context)
        if node.get('negated'):
            q = ~q
        return q
    # group
    op = node['op']
    children = node.get('children') or []
    if not children:
        return Q()
    qs = [_compile_node(c, fields, context) for c in children]
    result = qs[0]
    for q in qs[1:]:
        result = (result | q) if op == 'OR' else (result & q)
    if node.get('negated'):
        result = ~result
    return result


# ── Сериализация AST → CQL-строка ─────────────────────────────────────────────
_BARE_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_NUMBER_RE = re.compile(r'^-?\d+(?:\.\d+)?$')


def _format_value(v) -> str:
    if v is None:
        return 'NULL'
    if v == '__ME__':
        return 'me'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if _NUMBER_RE.match(s):
        return s
    if _is_date_dsl(s) or _ISO_DATE_RE.match(s):
        return s
    if _BARE_IDENT_RE.match(s):
        return s
    return '"' + s.replace('"', '\\"') + '"'


def _serialize_node(node: dict, parent_op: str = None) -> str:
    if node['type'] == 'condition':
        f = node['field']
        op = node['op']
        v = node['value']
        if op in ('IS NULL', 'IS NOT NULL'):
            base = f'{f} {op}'
        elif op in ('IN', 'NOT IN'):
            inside = ', '.join(_format_value(x) for x in (v or []))
            base = f'{f} {op} ({inside})'
        elif op in ('BETWEEN', 'NOT BETWEEN'):
            lo, hi = (v or [None, None])[:2]
            base = f'{f} {op} {_format_value(lo)} AND {_format_value(hi)}'
        elif op == 'CONTAINS':
            base = f'{f} CONTAINS {_format_value(v)}'
        else:
            base = f'{f} {op} {_format_value(v)}'
        if node.get('negated'):
            base = f'NOT ({base})'
        return base

    # group
    op = node['op']
    children = node.get('children') or []
    if not children:
        return ''
    parts = [_serialize_node(c, op) for c in children]
    parts = [p for p in parts if p]
    if not parts:
        return ''
    if len(parts) == 1:
        joined = parts[0]
    else:
        joined = (' ' + op + ' ').join(parts)
    need_parens = (
        node.get('negated') or
        (parent_op is not None and parent_op != op and len(parts) > 1) or
        (parent_op == 'AND' and op == 'OR')
    )
    out = f'({joined})' if need_parens else joined
    if node.get('negated'):
        out = f'NOT {out}'
    return out


# ── Публичный API ─────────────────────────────────────────────────────────────

def parse_to_ast(query: str, entity_type: str = 'cargo') -> dict:
    """Парсит CQL-строку и возвращает JSON-совместимый AST.
    Пустая строка → пустая корневая AND-группа."""
    query = (query or '').strip()
    fields = _normalize_fields(HAWB_FIELDS if entity_type == 'hawb' else CARGO_FIELDS)
    if not query:
        return _group('AND', [])
    try:
        tokens = _tokenize(query)
        return _Parser(tokens, fields).parse()
    except CQLError:
        raise
    except Exception as exc:
        raise CQLError(f'Ошибка разбора: {exc}') from exc


def compile_ast(ast: dict, context: dict = None, entity_type: str = 'cargo') -> Q:
    """Компилирует AST в Django Q-объект."""
    fields = _normalize_fields(HAWB_FIELDS if entity_type == 'hawb' else CARGO_FIELDS)
    if not ast or not (ast.get('children') if ast.get('type') == 'group' else True):
        return Q()
    return _compile_node(ast, fields, context or {})


def serialize_ast(ast: dict) -> str:
    """Сериализует AST обратно в CQL-строку."""
    if not ast:
        return ''
    return _serialize_node(ast)


def parse_cql(query: str, context: dict = None, entity_type: str = 'cargo') -> Q:
    """Совместимое API: парсит CQL и возвращает Q.
    Эквивалентно compile_ast(parse_to_ast(query, entity_type), context, entity_type).

    Пустая строка → Q() (без фильтра).
    """
    query = (query or '').strip()
    if not query:
        return Q()
    return compile_ast(parse_to_ast(query, entity_type), context, entity_type)


# ── Справка по полям (для отображения в UI) ───────────────────────────────────
CARGO_FIELD_REFERENCE = [
    ('stage',               'Этап партии',           'stage = ARRIVED', 'DRAFT · FORMED · DISPATCHED · ARRIVED · CUSTOMS · RELEASED'),
    ('weight',              'Вес, кг',               'weight > 100', ''),
    ('pieces_declared',     'Мест',                   'pieces_declared >= 5', 'алиас: pieces'),
    ('flight_date',         'Дата прилёта',           'flight_date >= 2024-01-01', 'ГГГГ-ММ-ДД, или -7d / today()'),
    ('departure_date',      'Дата отправки',          'departure_date >= -7d', 'ГГГГ-ММ-ДД, или -7d / today()'),
    ('warehouse',           'Лицензия СВХ',           'warehouse = "ШРМ"', ''),
    ('warehouse_name',      'Название СВХ',           'warehouse_name CONTAINS "Шереметьево"', ''),
    ('assigned_to',         'Назначено на',           'assigned_to = me', 'me = текущий пользователь'),
    ('awb',                 'Номер AWB',              'awb CONTAINS "123"', ''),
    ('description',         'Описание груза',         'description CONTAINS "ноутбук"', ''),
    ('customs_declaration', 'Номер ДТ',               'customs_declaration CONTAINS "10005"', ''),
    ('departure_iata',      'IATA отправки',          'departure_iata = "AMS"', '3 буквы'),
    ('arrival_iata',        'IATA прибытия',          'arrival_iata = "SVO"', '3 буквы'),
    ('shp_type',            'Тип отправления',        'shp_type = B2C', 'IMPEX · B2C · B2B · DIP'),
    ('invoice_value',       'Стоимость инвойса',      'invoice_value BETWEEN 100 AND 500', ''),
    ('is_draft',            'Черновик',               'is_draft = true', 'true / false'),
    ('is_self_clearance',   'ТО клиентом',            'is_self_clearance = true', 'клиент таможит сам'),
    ('is_transit',          'Транзитный груз',        'is_transit = true', 'true / false'),
    ('is_problematic',      'Проблемная партия',      'is_problematic = true', 'недостача или >7 дней на СВХ'),
    ('days_in_warehouse',   'Дней на СВХ',            'days_in_warehouse > 7', ''),
    ('transport_mode',      'Вид транспорта',         'transport_mode = 4', '1-Море · 2-ЖД · 3-Авто · 4-Авиа · 5-Почта'),
    ('hawb_number',         'Номер HAWB в партии',     'hawb_number CONTAINS "SA355"', 'обратный поиск по накладным'),
    ('hawb_consignee',      'Получатель HAWB',         'hawb_consignee CONTAINS "ООО"', 'обратный поиск по накладным'),
    ('hawb_status',         'Лог. статус HAWB',         'hawb_status = AT_SVH', 'CREATED · AT_ORIGIN_WH · IN_TRANSIT_EXP · AT_SVH · IMPORT_CUSTOMS · DELIVERED · …'),
    ('hawb_cargo_type',     'Тип груза HAWB',           'hawb_cargo_type = B2B', 'B2C · B2B · C2C · DOC'),
    ('labels',              'Метки',                   'labels IN ("СРОЧНО", "В РАБОТЕ")', 'IS NULL — без меток; IN/NOT IN — пересечение'),
]

HAWB_FIELD_REFERENCE = [
    ('hawb_number',         'Номер HAWB',             'hawb_number CONTAINS "SA355"', ''),
    ('description',         'Описание груза',          'description CONTAINS "ноутбук"', ''),
    ('cargo_type',          'Тип груза',              'cargo_type = B2B', 'B2C · B2B · C2C · DOC'),
    ('shipment_type',       'Направление',            'shipment_type = IMPORT', 'IMPORT · EXPORT'),
    ('consignee',           'Получатель',             'consignee CONTAINS "ООО"', ''),
    ('consignee_city',      'Город получателя',        'consignee_city = "Москва"', ''),
    ('consignee_inn',       'ИНН получателя',          'consignee_inn = "7707123456"', ''),
    ('logistics_status',    'Логистический статус',   'logistics_status = AT_SVH', 'CREATED · AT_ORIGIN_WH · IN_TRANSIT_EXP · AT_SVH · IMPORT_CUSTOMS · READY_DELIVERY · DELIVERED · …'),
    ('customs_status',      'Таможенный статус',       'customs_status = RELEASED', 'NOT_REQUIRED · PENDING · DECLARED · RELEASED · …'),
    ('weight',              'Вес, кг',                'weight BETWEEN 100 AND 500', ''),
    ('pieces_declared',     'Мест',                    'pieces_declared >= 5', 'алиас: pieces'),
    ('invoice_value',       'Стоимость инвойса',       'invoice_value > 1000', ''),
    ('customs_declaration', 'Номер ДТ',                'customs_declaration CONTAINS "10005"', ''),
    ('release_date',        'Дата выпуска',            'release_date >= -7d', 'ГГГГ-ММ-ДД, или -7d / today()'),
    ('assigned_to',         'Назначено на',            'assigned_to = me', 'me = текущий пользователь'),
    ('mawb',                'Номер MAWB (партии)',     'mawb CONTAINS "020-"', ''),
    ('is_standalone',       'Без партии (сиротская)',  'is_standalone = true', 'true / false — накладная не привязана к MAWB'),
    ('warehouse',           'Лицензия СВХ (из партии)','warehouse = "ШРМ"', 'берётся из MAWB'),
    ('warehouse_name',      'Название СВХ (из партии)','warehouse_name CONTAINS "Шереметьево"', 'берётся из MAWB'),
    ('days_in_warehouse',   'Дней на СВХ',             'days_in_warehouse > 7', ''),
    ('labels',              'Метки',                   'labels IS NULL', 'IS NULL — без меток; IN/NOT IN — пересечение'),
]

# Алиас для обратной совместимости
FIELD_REFERENCE = CARGO_FIELD_REFERENCE


def get_field_reference(entity_type: str = 'cargo') -> list:
    """Возвращает справочник полей для указанной сущности."""
    return HAWB_FIELD_REFERENCE if entity_type == 'hawb' else CARGO_FIELD_REFERENCE


# ── Whitelists для pivot-виджета ─────────────────────────────────────────────
# Метки сознательно не добавляем в GROUPABLE_FIELDS — M2M в group by даёт дубли строк.

def _hawb_status_choices():
    from .models import HouseWaybill
    return dict(HouseWaybill.LOGISTICS_STATUS_CHOICES)


def _hawb_customs_choices():
    from .models import HouseWaybill
    return dict(HouseWaybill.CUSTOMS_STATUS_CHOICES)


_STAGE_CHOICES_LABEL = {
    'DRAFT': 'Формирование', 'FORMED': 'Сформирована',
    'DISPATCHED': 'Отправлена', 'ARRIVED': 'Прибыла',
    'CUSTOMS': 'Таможня', 'RELEASED': 'Выпущена',
}
_TRANSPORT_MODE_LABEL = {
    1: 'Море', 2: 'ЖД', 3: 'Авто', 4: 'Авиа', 5: 'Почта',
}
_CARGO_TYPE_LABEL = {
    'B2C': 'B2C', 'B2B': 'B2B', 'C2C': 'C2C', 'DOC': 'DOC',
}
_SHIPMENT_TYPE_LABEL = {'IMPORT': 'Импорт', 'EXPORT': 'Экспорт'}

GROUPABLE_FIELDS = {
    'cargo': {
        'warehouse':      {'orm': 'warehouse_license', 'label': 'Лицензия СВХ',
                           'label_orm': 'warehouse_name'},
        'warehouse_name': {'orm': 'warehouse_name',    'label': 'Название СВХ'},
        'stage':          {'orm': 'stage',             'label': 'Этап',
                           'choices': _STAGE_CHOICES_LABEL},
        'shp_type':       {'orm': 'shp_type',          'label': 'Тип отправления'},
        'departure_iata': {'orm': 'departure_iata',    'label': 'IATA отправки'},
        'arrival_iata':   {'orm': 'arrival_iata',      'label': 'IATA прибытия'},
        'is_draft':       {'orm': 'is_draft',          'label': 'Черновик',
                           'choices': {True: 'Да', False: 'Нет'}},
        'is_self_clearance': {'orm': 'is_self_clearance', 'label': 'ТО клиентом',
                              'choices': {True: 'ТО клиентом', False: 'Наше ТО'}},
        'transport_mode': {'orm': 'transportation_mode', 'label': 'Вид транспорта',
                           'choices': _TRANSPORT_MODE_LABEL},
    },
    'hawb': {
        'warehouse':        {'orm': 'mawb__warehouse_license', 'label': 'Лицензия СВХ',
                             'label_orm': 'mawb__warehouse_name'},
        'logistics_status': {'orm': 'logistics_status',  'label': 'Лог. статус',
                             'choices_fn': _hawb_status_choices},
        'customs_status':   {'orm': 'customs_status',    'label': 'Там. статус',
                             'choices_fn': _hawb_customs_choices},
        'cargo_type':       {'orm': 'cargo_type',        'label': 'Тип груза',
                             'choices': _CARGO_TYPE_LABEL},
        'shipment_type':    {'orm': 'shipment_type',     'label': 'Направление',
                             'choices': _SHIPMENT_TYPE_LABEL},
        'consignee_city':   {'orm': 'consignee_city',    'label': 'Город получателя'},
        'assigned_to':      {'orm': 'assigned_to__username', 'label': 'Назначено на'},
        'is_standalone':    {'orm': '__mawb_isnull__',   'label': 'Без партии',
                             'choices': {True: 'Без партии', False: 'Внутри партии'}},
    },
}

AGGREGATABLE_FIELDS = {
    'cargo': {
        'weight':          {'orm': 'weight',          'label': 'Вес, кг',        'type': 'num'},
        'pieces_declared': {'orm': 'pieces_declared', 'label': 'Мест',            'type': 'num'},
        'invoice_value':   {'orm': 'invoice_value',   'label': 'Стоимость инвойса','type': 'num'},
        # Связанные сущности (reverse FK / FK) — считаются через count_distinct
        'hawb_count':      {'orm': 'hawbs',           'label': 'Кол-во HAWB',     'type': 'num',
                            'count_only': True},
    },
    'hawb': {
        'weight':          {'orm': 'weight',          'label': 'Вес, кг',         'type': 'num'},
        'pieces_declared': {'orm': 'pieces_declared', 'label': 'Мест',             'type': 'num'},
        'invoice_value':   {'orm': 'invoice_value',   'label': 'Стоимость инвойса','type': 'num'},
        # Связанная сущность MAWB — уникальные MAWB внутри группы HAWB
        'mawb_count':      {'orm': 'mawb',            'label': 'Кол-во MAWB',      'type': 'num',
                            'count_only': True},
    },
}

ALLOWED_AGGS = {'count', 'sum', 'avg', 'count_distinct', 'ratio'}


def get_groupable_fields(entity_type: str = 'cargo') -> dict:
    return GROUPABLE_FIELDS.get(entity_type, GROUPABLE_FIELDS['cargo'])


def get_aggregatable_fields(entity_type: str = 'cargo') -> dict:
    return AGGREGATABLE_FIELDS.get(entity_type, AGGREGATABLE_FIELDS['cargo'])
