/**
 * CQL Tree Builder — визуальный конструктор фильтров CQL в стиле Jira.
 *
 * Использование:
 *   const inst = CQLBuilder.init({
 *     container: HTMLElement,        // куда монтировать дерево
 *     textarea:  HTMLTextAreaElement,// связанное поле для текстового режима
 *     entity:    'cargo' | 'hawb',
 *     defs:      {field: {t, opts}}, // CQL_DEFS_CARGO или CQL_DEFS_HAWB
 *     getMe:     () => 'username',
 *     onChange:  (cqlStr) => {},     // вызывается после изменения
 *     onValidity:(state)  => {},     // {ok: bool, error?: str}
 *     csrftoken: string,
 *   });
 *   inst.setCQL("stage = ARRIVED");  // через server-side parse-tree
 *   inst.getCQL();                   // -> string
 *   inst.setEntity('hawb');
 *   inst.switchMode('text' | 'builder');
 *   inst.destroy();
 */
(function() {
  'use strict';

  const MAX_DEPTH = 4;
  const VALIDATE_DEBOUNCE = 250;
  const CHANGE_DEBOUNCE = 150;

  // ── Метаданные операторов по типу поля ─────────────────────────────────────
  const OPS_BY_TYPE = {
    str:  ['=', '!=', 'CONTAINS', '~', '!~', 'IN', 'NOT IN', 'IS NULL', 'IS NOT NULL'],
    num:  ['=', '!=', '>', '>=', '<', '<=', 'BETWEEN', 'IN', 'NOT IN', 'IS NULL', 'IS NOT NULL'],
    date: ['=', '!=', '>', '>=', '<', '<=', 'BETWEEN', 'IS NULL', 'IS NOT NULL'],
    bool: ['=', '!='],
    multi: ['IN', 'NOT IN', 'IS NULL', 'IS NOT NULL', '=', '!='],  // для labels
  };

  // Спец-поля, для которых блокируем некоторые операторы
  const BLOCKED_OPS_FOR_FIELD = {
    is_problematic:    new Set(['IN', 'NOT IN', 'IS NULL', 'IS NOT NULL', 'BETWEEN', 'CONTAINS', '~', '!~']),
    is_standalone:     new Set(['IN', 'NOT IN', 'IS NULL', 'IS NOT NULL', 'BETWEEN', 'CONTAINS', '~', '!~']),
    days_in_warehouse: new Set(['IN', 'NOT IN', 'IS NULL', 'IS NOT NULL', 'CONTAINS', '~', '!~']),
  };

  // ── Утилиты ──────────────────────────────────────────────────────────────
  function uid() { return 'n_' + Math.random().toString(36).slice(2, 9); }
  function escHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g,
      c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function debounce(fn, ms) {
    let t = null;
    return function(...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), ms);
    };
  }
  function deepClone(o) { return JSON.parse(JSON.stringify(o)); }

  // ── Сериализация AST → CQL ────────────────────────────────────────────────
  const BARE_IDENT_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
  const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  const NUMBER_RE = /^-?\d+(?:\.\d+)?$/;
  const DATE_DSL_RE = /^([+-]\d+[dwMhy]|now\(\)|today\(\)|startOfDay\(\)|endOfDay\(\))$/;

  function formatValue(v) {
    if (v === null || v === undefined) return 'NULL';
    if (v === '__ME__') return 'me';
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    if (typeof v === 'number') return String(v);
    const s = String(v);
    if (NUMBER_RE.test(s)) return s;
    if (ISO_DATE_RE.test(s) || DATE_DSL_RE.test(s)) return s;
    if (BARE_IDENT_RE.test(s)) return s;
    return '"' + s.replace(/"/g, '\\"') + '"';
  }

  function serializeCondition(c) {
    const f = c.field, op = c.op, v = c.value;
    let base;
    if (op === 'IS NULL' || op === 'IS NOT NULL') {
      base = `${f} ${op}`;
    } else if (op === 'IN' || op === 'NOT IN') {
      const arr = Array.isArray(v) ? v : (v == null ? [] : [v]);
      base = `${f} ${op} (${arr.map(formatValue).join(', ')})`;
    } else if (op === 'BETWEEN' || op === 'NOT BETWEEN') {
      const [lo, hi] = Array.isArray(v) ? v : [null, null];
      base = `${f} ${op} ${formatValue(lo)} AND ${formatValue(hi)}`;
    } else if (op === 'CONTAINS') {
      base = `${f} CONTAINS ${formatValue(v)}`;
    } else {
      base = `${f} ${op} ${formatValue(v)}`;
    }
    return c.negated ? `NOT (${base})` : base;
  }

  function serializeNode(node, parentOp = null) {
    if (node.type === 'condition') {
      // Skip incomplete conditions
      if (!node.field || node.op == null) return '';
      if ((node.op === 'IN' || node.op === 'NOT IN')
          && (!Array.isArray(node.value) || node.value.length === 0)) return '';
      return serializeCondition(node);
    }
    // group
    const op = node.op;
    const children = node.children || [];
    if (children.length === 0) return '';
    const parts = children.map(c => serializeNode(c, op)).filter(Boolean);
    if (parts.length === 0) return '';
    if (parts.length === 1) {
      const inner = parts[0];
      return node.negated ? `NOT (${inner})` : inner;
    }
    const joined = parts.join(' ' + op + ' ');
    const needParens =
      node.negated ||
      (parentOp !== null && parentOp !== op) ||
      (parentOp === 'AND' && op === 'OR');
    const out = needParens ? `(${joined})` : joined;
    return node.negated ? `NOT ${out}` : out;
  }

  // ── Builder: создаём дерево по умолчанию ───────────────────────────────
  function defaultTree() {
    return {type: 'group', op: 'AND', negated: false, children: []};
  }

  function newCondition(defs) {
    const firstField = Object.keys(defs)[0] || '';
    return {
      type: 'condition',
      id: uid(),
      field: firstField,
      op: '=',
      value: '',
      negated: false,
    };
  }

  function newGroup(parentOp) {
    return {
      type: 'group',
      id: uid(),
      op: parentOp === 'AND' ? 'OR' : 'AND',
      negated: false,
      children: [],
    };
  }

  // Глубина дерева
  function depthOf(node) {
    if (node.type !== 'group') return 0;
    const c = node.children || [];
    if (c.length === 0) return 1;
    return 1 + Math.max(...c.map(depthOf));
  }

  // ── Рендеринг ──────────────────────────────────────────────────────────
  function renderToolbar(state) {
    const isText = state.mode === 'text';
    return `
      <div class="cql-tree-toolbar d-flex justify-content-between align-items-center mb-2">
        <div class="btn-group btn-group-sm" role="group">
          <button type="button" class="btn btn-outline-secondary ${isText ? '' : 'active'}" data-mode-btn="builder">
            <i class="bi bi-diagram-3"></i> Конструктор
          </button>
          <button type="button" class="btn btn-outline-secondary ${isText ? 'active' : ''}" data-mode-btn="text">
            <i class="bi bi-code-slash"></i> Текст
          </button>
        </div>
        <div class="cql-tree-status text-muted small" data-status></div>
      </div>
    `;
  }

  function renderGroup(node, depth, isRoot) {
    const childCount = (node.children || []).length;
    const atMaxDepth = depth >= MAX_DEPTH;
    return `
      <div class="cql-group card border-secondary mb-2" data-node-id="${node.id}" data-node-type="group">
        <div class="card-header py-1 px-2 d-flex align-items-center gap-2 bg-light">
          <div class="btn-group btn-group-sm" role="group">
            <button type="button" class="btn btn-outline-primary ${node.op === 'AND' ? 'active' : ''}" data-group-op="AND">AND</button>
            <button type="button" class="btn btn-outline-primary ${node.op === 'OR' ? 'active' : ''}" data-group-op="OR">OR</button>
          </div>
          <div class="form-check form-check-inline mb-0">
            <input class="form-check-input" type="checkbox" data-group-not ${node.negated ? 'checked' : ''} id="${node.id}_not">
            <label class="form-check-label small" for="${node.id}_not">NOT</label>
          </div>
          <span class="text-muted small ms-auto" data-child-count>${childCount} ${pluralize(childCount, 'условие', 'условия', 'условий')}</span>
          ${isRoot ? '' : `
            <button type="button" class="btn btn-sm btn-outline-danger ms-2" data-group-remove title="Удалить группу">
              <i class="bi bi-x-lg"></i>
            </button>
          `}
        </div>
        <div class="card-body p-2" data-group-children></div>
        <div class="card-footer py-1 px-2 d-flex gap-2 bg-white">
          <button type="button" class="btn btn-sm btn-outline-secondary" data-add-condition>
            <i class="bi bi-plus"></i> Условие
          </button>
          <button type="button" class="btn btn-sm btn-outline-secondary" data-add-group ${atMaxDepth ? 'disabled title="Достигнут максимум вложенности"' : ''}>
            <i class="bi bi-plus-square"></i> Подгруппа
          </button>
        </div>
      </div>
    `;
  }

  function pluralize(n, one, few, many) {
    const a = Math.abs(n) % 100;
    const b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
  }

  function renderCondition(node, defs) {
    const fieldOpts = renderFieldOptions(defs, node.field);
    const fieldDef = defs[node.field];
    const ops = allowedOps(node.field, fieldDef);
    const opOpts = ops.map(op =>
      `<option value="${op}" ${node.op === op ? 'selected' : ''}>${op}</option>`
    ).join('');
    return `
      <div class="cql-condition row g-1 align-items-start mb-1" data-node-id="${node.id}" data-node-type="condition">
        <div class="col-12 col-md-4">
          <select class="form-select form-select-sm" data-cond-field>${fieldOpts}</select>
        </div>
        <div class="col-6 col-md-2">
          <select class="form-select form-select-sm" data-cond-op>${opOpts}</select>
        </div>
        <div class="col-12 col-md-5" data-cond-value-wrap>
          ${renderValueInput(node, fieldDef)}
        </div>
        <div class="col-6 col-md-1 text-end">
          <button type="button" class="btn btn-sm btn-outline-danger" data-cond-remove title="Удалить условие">
            <i class="bi bi-trash"></i>
          </button>
        </div>
      </div>
    `;
  }

  function renderFieldOptions(defs, current) {
    let html = '';
    if (current && !defs[current]) {
      html += `<option value="${escHtml(current)}" selected>${escHtml(current)} (нет в справочнике)</option>`;
    }
    for (const f of Object.keys(defs)) {
      const def = defs[f];
      const label = def.label || f;
      html += `<option value="${escHtml(f)}" ${current === f ? 'selected' : ''}>${escHtml(label)}</option>`;
    }
    return html;
  }

  function allowedOps(fieldName, fieldDef) {
    if (!fieldDef) return OPS_BY_TYPE.str;
    const t = fieldDef.t || 'str';
    let ops = OPS_BY_TYPE[t] || OPS_BY_TYPE.str;
    const blocked = BLOCKED_OPS_FOR_FIELD[fieldName];
    if (blocked) {
      ops = ops.filter(op => !blocked.has(op));
    }
    return ops;
  }

  function renderValueInput(node, fieldDef) {
    const op = node.op;
    const v = node.value;
    if (op === 'IS NULL' || op === 'IS NOT NULL') {
      return '<div class="form-text small fst-italic mt-1">— нет значения —</div>';
    }
    const t = fieldDef ? (fieldDef.t || 'str') : 'str';
    const isLabels = fieldDef && fieldDef.t === 'multi';

    if (op === 'BETWEEN' || op === 'NOT BETWEEN') {
      const [lo, hi] = Array.isArray(v) ? v : ['', ''];
      const inputType = (t === 'date') ? 'date' : (t === 'num' ? 'number' : 'text');
      return `
        <div class="d-flex gap-1 align-items-center">
          <input class="form-control form-control-sm" type="${inputType}" data-cond-value-lo value="${escHtml(lo ?? '')}" placeholder="от">
          <span class="text-muted small">—</span>
          <input class="form-control form-control-sm" type="${inputType}" data-cond-value-hi value="${escHtml(hi ?? '')}" placeholder="до">
        </div>
      `;
    }

    if (op === 'IN' || op === 'NOT IN') {
      const arr = Array.isArray(v) ? v : (v ? [v] : []);
      if (isLabels) {
        // Labels: multi-select с lazy-load (фактическая загрузка — после mount)
        return `
          <select class="form-select form-select-sm" data-cond-value multiple size="4" data-labels-multi>
            ${arr.map(x => `<option value="${escHtml(x)}" selected>${escHtml(x)}</option>`).join('')}
          </select>
          <div class="form-text small">Выберите одну или несколько меток</div>
        `;
      }
      if (fieldDef && fieldDef.opts) {
        return `
          <select class="form-select form-select-sm" data-cond-value multiple size="4">
            ${fieldDef.opts.map(([val, lbl]) =>
              `<option value="${escHtml(val)}" ${arr.includes(val) ? 'selected' : ''}>${escHtml(lbl)}</option>`
            ).join('')}
          </select>
        `;
      }
      // Свободный список через запятую (с подсказками для str без opts)
      const suggestable = fieldDef && fieldDef.suggest && t === 'str';
      const dlAttrIn = suggestable ? `list="dl_${node.id}_in"` : '';
      const dlNodeIn = suggestable ? `<datalist id="dl_${node.id}_in" data-cond-suggest></datalist>` : '';
      return `<input class="form-control form-control-sm" type="text" data-cond-value ${dlAttrIn} value="${escHtml(arr.join(', '))}" placeholder="через запятую">${dlNodeIn}`;
    }

    if (isLabels) {
      // Single equality на label
      return renderSingleSelect(fieldDef, v, true);
    }
    if (fieldDef && fieldDef.opts) {
      return renderSingleSelect(fieldDef, v, false);
    }
    if (t === 'date') {
      // Composite: input type=date или DSL
      const isDsl = DATE_DSL_RE.test(v);
      return `
        <div class="d-flex gap-1 align-items-center">
          <input class="form-control form-control-sm" type="${isDsl ? 'text' : 'date'}" data-cond-value value="${escHtml(v ?? '')}" placeholder="${isDsl ? '-7d, today(), now()' : 'YYYY-MM-DD'}">
          <button type="button" class="btn btn-sm btn-outline-secondary" data-toggle-dsl title="${isDsl ? 'Календарь' : 'Относительная дата'}">${isDsl ? '📅' : 'DSL'}</button>
        </div>
      `;
    }
    if (t === 'num') {
      return `<input class="form-control form-control-sm" type="number" step="any" data-cond-value value="${escHtml(v ?? '')}">`;
    }
    if (t === 'bool') {
      return `
        <select class="form-select form-select-sm" data-cond-value>
          <option value="true" ${v === 'true' || v === true ? 'selected' : ''}>true</option>
          <option value="false" ${v === 'false' || v === false ? 'selected' : ''}>false</option>
        </select>
      `;
    }
    // str без opts — подсказки из БД, если поле помечено suggest:true
    const suggestable = fieldDef && fieldDef.suggest;
    const dlAttr = suggestable ? `list="dl_${node.id}"` : '';
    const dlNode = suggestable
      ? `<datalist id="dl_${node.id}" data-cond-suggest></datalist>` : '';
    return `<input class="form-control form-control-sm" type="text" data-cond-value ${dlAttr} value="${escHtml(v ?? '')}" placeholder="${suggestable ? 'начните вводить — появятся подсказки' : ''}">${dlNode}`;
  }

  function renderSingleSelect(fieldDef, current, isLabels) {
    const opts = (fieldDef && fieldDef.opts) || [];
    const placeholder = isLabels ? 'Выберите метку' : '— выберите —';
    return `
      <select class="form-select form-select-sm" data-cond-value ${isLabels ? 'data-labels-single' : ''}>
        <option value="">${placeholder}</option>
        ${opts.map(([val, lbl]) =>
          `<option value="${escHtml(val)}" ${String(current) === String(val) ? 'selected' : ''}>${escHtml(lbl)}</option>`
        ).join('')}
        ${current && !opts.some(o => String(o[0]) === String(current))
          ? `<option value="${escHtml(current)}" selected>${escHtml(current)}</option>`
          : ''}
      </select>
    `;
  }

  // ── Конвертация значения при смене оператора ───────────────────────────
  function adjustValueForOp(oldOp, newOp, oldValue) {
    const wasArray = oldOp === 'IN' || oldOp === 'NOT IN';
    const wasBetween = oldOp === 'BETWEEN' || oldOp === 'NOT BETWEEN';
    const isArray = newOp === 'IN' || newOp === 'NOT IN';
    const isBetween = newOp === 'BETWEEN' || newOp === 'NOT BETWEEN';
    const isNull = newOp === 'IS NULL' || newOp === 'IS NOT NULL';

    if (isNull) return null;
    if (isArray) {
      if (wasArray) return oldValue;
      if (wasBetween) return Array.isArray(oldValue) ? oldValue : [];
      return oldValue ? [oldValue] : [];
    }
    if (isBetween) {
      if (wasBetween) return oldValue;
      if (wasArray) return Array.isArray(oldValue) ? oldValue.slice(0, 2) : ['', ''];
      return [oldValue || '', ''];
    }
    // scalar
    if (wasArray) return Array.isArray(oldValue) && oldValue.length ? oldValue[0] : '';
    if (wasBetween) return Array.isArray(oldValue) && oldValue.length ? oldValue[0] : '';
    return oldValue ?? '';
  }

  // ── Builder Instance ───────────────────────────────────────────────────
  class CQLBuilderInstance {
    constructor(opts) {
      this.opts = opts;
      this.tree = defaultTree();
      this.mode = 'builder';  // 'builder' | 'text'
      this.fallbackMode = false;  // если parse-tree не сработал
      this.validateSeq = 0;
      this.csrftoken = opts.csrftoken || '';
      this.labelCache = null;  // {id, name, color}[]
      this._internalUpdate = false;
      this._debouncedChange = debounce(() => this._notifyChange(), CHANGE_DEBOUNCE);
      this._debouncedValidate = debounce(() => this._validate(), VALIDATE_DEBOUNCE);
      this._mount();
      this._wireTextarea();
    }

    _mount() {
      const container = this.opts.container;
      container.innerHTML = `
        ${renderToolbar(this)}
        <div class="cql-tree-root" data-tree-root></div>
        <div class="cql-tree-error small text-danger mt-1" data-tree-error></div>
      `;
      // Перенесём textarea внутрь, под toolbar (чтобы было одно место)
      // Не двигаем — оставляем где есть, но прячем в режиме builder
      this.statusEl = container.querySelector('[data-status]');
      this.errorEl = container.querySelector('[data-tree-error]');
      this.rootEl = container.querySelector('[data-tree-root]');
      this._wireToolbar();
      this._renderTree();
      this._applyMode();
    }

    _wireToolbar() {
      const container = this.opts.container;
      container.querySelectorAll('[data-mode-btn]').forEach(btn => {
        btn.addEventListener('click', () => this.switchMode(btn.dataset.modeBtn));
      });
    }

    _wireTextarea() {
      const ta = this.opts.textarea;
      if (!ta) return;
      ta.addEventListener('input', () => {
        if (this._internalUpdate) return;
        this._debouncedValidate();
      });
      ta.addEventListener('blur', () => {
        if (this._internalUpdate) return;
        if (this.mode === 'text') {
          // При выходе из textarea пытаемся синхронизировать дерево
          this.setCQL(ta.value).catch(() => {});
        }
      });
    }

    _renderTree() {
      this.rootEl.innerHTML = this._renderNode(this.tree, 0, true);
      this._wireNodeEvents(this.rootEl, this.tree, true);
      this._lazyLoadLabels();
    }

    _renderNode(node, depth, isRoot) {
      if (node.type === 'group') {
        return renderGroup(node, depth, isRoot);
      }
      return renderCondition(node, this.opts.defs);
    }

    _wireNodeEvents(rootEl, node, isRoot) {
      // Привязываем события для группы node
      const groupEl = rootEl.querySelector(`.cql-group[data-node-id="${node.id}"]`);
      if (!groupEl || node.type !== 'group') return;

      // AND/OR
      groupEl.querySelectorAll(':scope > .card-header [data-group-op]').forEach(btn => {
        btn.addEventListener('click', () => {
          node.op = btn.dataset.groupOp;
          this._renderTree();
          this._notifyChangeNow();
        });
      });
      // NOT
      const notCb = groupEl.querySelector(':scope > .card-header [data-group-not]');
      if (notCb) {
        notCb.addEventListener('change', () => {
          node.negated = notCb.checked;
          this._notifyChangeNow();
        });
      }
      // Удалить группу
      const removeBtn = groupEl.querySelector(':scope > .card-header [data-group-remove]');
      if (removeBtn) {
        removeBtn.addEventListener('click', () => {
          if ((node.children || []).length > 1
              && !confirm('Удалить группу со всеми условиями?')) return;
          this._removeNode(node.id);
          this._renderTree();
          this._notifyChangeNow();
        });
      }
      // + Условие / + Подгруппа
      const addCondBtn = groupEl.querySelector(':scope > .card-footer [data-add-condition]');
      if (addCondBtn) {
        addCondBtn.addEventListener('click', () => {
          node.children.push(newCondition(this.opts.defs));
          this._renderTree();
          this._notifyChangeNow();
        });
      }
      const addGroupBtn = groupEl.querySelector(':scope > .card-footer [data-add-group]');
      if (addGroupBtn) {
        addGroupBtn.addEventListener('click', () => {
          if (addGroupBtn.disabled) return;
          node.children.push(newGroup(node.op));
          this._renderTree();
          this._notifyChangeNow();
        });
      }

      // Children
      const childContainer = groupEl.querySelector(':scope > [data-group-children]');
      if (!childContainer) return;
      // Очистим и перерендерим children (renderGroup рендерит пустой children-блок)
      childContainer.innerHTML = (node.children || [])
        .map(c => this._renderNode(c, this._depthFor(c), false))
        .join('');

      // Привязка событий для каждого ребёнка
      for (const child of node.children || []) {
        if (child.type === 'group') {
          this._wireNodeEvents(childContainer, child, false);
        } else {
          this._wireConditionEvents(childContainer, child);
        }
      }
    }

    _depthFor(node) {
      // Вычисляем глубину на основании родителя — упростим: просто считаем от рендеринга
      return depthOf(node);
    }

    _wireConditionEvents(rootEl, node) {
      const condEl = rootEl.querySelector(`.cql-condition[data-node-id="${node.id}"]`);
      if (!condEl) return;
      const fieldSel = condEl.querySelector('[data-cond-field]');
      const opSel = condEl.querySelector('[data-cond-op]');
      const valueWrap = condEl.querySelector('[data-cond-value-wrap]');
      const removeBtn = condEl.querySelector('[data-cond-remove]');

      // Поле
      fieldSel.addEventListener('change', () => {
        const newField = fieldSel.value;
        const oldDef = this.opts.defs[node.field];
        const newDef = this.opts.defs[newField];
        node.field = newField;
        // Если тип сменился — сбрасываем значение и оператор
        if (!newDef || !oldDef || newDef.t !== oldDef.t) {
          node.value = '';
          // Подбираем валидный op
          const ops = allowedOps(newField, newDef);
          if (!ops.includes(node.op)) node.op = ops[0] || '=';
        }
        this._refreshCondition(condEl, node);
        this._notifyChangeNow();
      });

      // Оператор
      opSel.addEventListener('change', () => {
        const oldOp = node.op;
        const newOp = opSel.value;
        node.op = newOp;
        node.value = adjustValueForOp(oldOp, newOp, node.value);
        // Перерендерим value-input (он зависит от op)
        valueWrap.innerHTML = renderValueInput(node, this.opts.defs[node.field]);
        this._wireValueInput(valueWrap, node);
        this._notifyChangeNow();
      });

      // Value
      this._wireValueInput(valueWrap, node);

      // Удалить
      removeBtn.addEventListener('click', () => {
        this._removeNode(node.id);
        this._renderTree();
        this._notifyChangeNow();
      });
    }

    _refreshCondition(condEl, node) {
      // Перестраиваем optгруппу operators и value-input, не трогая поле-select (фокус)
      const opSel = condEl.querySelector('[data-cond-op]');
      const fieldDef = this.opts.defs[node.field];
      const ops = allowedOps(node.field, fieldDef);
      opSel.innerHTML = ops.map(op =>
        `<option value="${op}" ${node.op === op ? 'selected' : ''}>${op}</option>`
      ).join('');
      const valueWrap = condEl.querySelector('[data-cond-value-wrap]');
      valueWrap.innerHTML = renderValueInput(node, fieldDef);
      this._wireValueInput(valueWrap, node);
      // Подгрузка labels, если поле = labels
      this._lazyLoadLabels();
    }

    _wireValueInput(wrap, node) {
      const lo = wrap.querySelector('[data-cond-value-lo]');
      const hi = wrap.querySelector('[data-cond-value-hi]');
      if (lo && hi) {
        const upd = () => {
          node.value = [lo.value, hi.value];
          this._notifyChange();
        };
        lo.addEventListener('input', upd);
        hi.addEventListener('input', upd);
        return;
      }
      const v = wrap.querySelector('[data-cond-value]');
      if (v) {
        const upd = () => {
          if (v.tagName === 'SELECT' && v.multiple) {
            node.value = Array.from(v.selectedOptions).map(o => o.value);
          } else if (node.op === 'IN' || node.op === 'NOT IN') {
            // Текст через запятую
            node.value = String(v.value || '').split(',').map(s => s.trim()).filter(Boolean);
          } else {
            node.value = v.value;
          }
          this._notifyChange();
        };
        v.addEventListener('input', upd);
        v.addEventListener('change', upd);
      }
      // Подсказки из БД (datalist рядом с input)
      const dl = wrap.querySelector('datalist[data-cond-suggest]');
      if (dl && v && v.tagName === 'INPUT') {
        const fetchSuggest = debounce(() => this._fetchSuggestions(dl, node, v.value), 200);
        v.addEventListener('focus', () => this._fetchSuggestions(dl, node, v.value));
        v.addEventListener('input', fetchSuggest);
      }
      // DSL toggle
      const dslBtn = wrap.querySelector('[data-toggle-dsl]');
      if (dslBtn) {
        dslBtn.addEventListener('click', () => {
          const inp = wrap.querySelector('[data-cond-value]');
          if (!inp) return;
          if (inp.type === 'date') {
            inp.type = 'text';
            inp.placeholder = '-7d, today(), now()';
            dslBtn.textContent = '📅';
          } else {
            inp.type = 'date';
            inp.placeholder = 'YYYY-MM-DD';
            inp.value = '';
            node.value = '';
            dslBtn.textContent = 'DSL';
          }
        });
      }
    }

    _lazyLoadLabels() {
      const targets = this.opts.container.querySelectorAll('[data-labels-multi], [data-labels-single]');
      if (targets.length === 0) return;
      const apply = (labels) => {
        targets.forEach(sel => {
          const isMulti = sel.hasAttribute('data-labels-multi');
          const current = isMulti
            ? Array.from(sel.selectedOptions).map(o => o.value)
            : [sel.value];
          if (isMulti) {
            sel.innerHTML = labels.map(l =>
              `<option value="${escHtml(l.name)}" ${current.includes(l.name) ? 'selected' : ''}>${escHtml(l.name)}</option>`
            ).join('');
          } else {
            sel.innerHTML = '<option value="">— выберите —</option>' +
              labels.map(l =>
                `<option value="${escHtml(l.name)}" ${current.includes(l.name) ? 'selected' : ''}>${escHtml(l.name)}</option>`
              ).join('');
          }
          // Снимаем маркеры, чтобы не подгружать повторно
          sel.removeAttribute('data-labels-multi');
          sel.removeAttribute('data-labels-single');
          sel.setAttribute('data-labels-loaded', '1');
        });
      };
      if (this.labelCache) {
        apply(this.labelCache);
      } else {
        fetch('/api/v1/labels/', {credentials: 'same-origin'})
          .then(r => r.json())
          .then(d => {
            this.labelCache = d.labels || [];
            apply(this.labelCache);
          })
          .catch(() => apply([]));
      }
    }

    _fetchSuggestions(datalistEl, node, query) {
      // Кэш на (entity, field, q) чтобы не дёргать API повторно
      this._suggestCache = this._suggestCache || new Map();
      const entity = this.opts.entity || 'cargo';
      const q = String(query || '').trim();
      const key = `${entity}|${node.field}|${q}`;
      const fill = (vals) => {
        datalistEl.innerHTML = (vals || [])
          .map(v => `<option value="${escHtml(v)}"></option>`)
          .join('');
      };
      if (this._suggestCache.has(key)) {
        fill(this._suggestCache.get(key));
        return;
      }
      const params = new URLSearchParams({entity, field: node.field, q, limit: '20'});
      fetch(`/api/v1/dashboard/cql/values/?${params.toString()}`, {credentials: 'same-origin'})
        .then(r => r.ok ? r.json() : {values: []})
        .then(d => {
          const vals = Array.isArray(d.values) ? d.values : [];
          this._suggestCache.set(key, vals);
          fill(vals);
        })
        .catch(() => fill([]));
    }

    _removeNode(id) {
      function rec(group) {
        group.children = (group.children || []).filter(c => {
          if (c.id === id) return false;
          if (c.type === 'group') rec(c);
          return true;
        });
      }
      rec(this.tree);
    }

    _notifyChange() {
      this._debouncedChange.cancel && this._debouncedChange.cancel();
      this._notifyChangeNow();
    }

    _notifyChangeNow() {
      const cql = this.getCQL();
      this._writeTextarea(cql);
      if (this.opts.onChange) this.opts.onChange(cql);
      this._debouncedValidate();
    }

    _writeTextarea(cql) {
      const ta = this.opts.textarea;
      if (!ta) return;
      this._internalUpdate = true;
      ta.value = cql;
      this._internalUpdate = false;
    }

    _validate() {
      const cql = this.getCQL();
      const seq = ++this.validateSeq;
      if (!cql) {
        this.statusEl.textContent = '';
        this.statusEl.className = 'cql-tree-status text-muted small';
        this.errorEl.textContent = '';
        if (this.opts.onValidity) this.opts.onValidity({ok: true});
        return;
      }
      fetch('/api/v1/dashboard/cql/validate/', {
        method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': this.csrftoken},
        body: JSON.stringify({query: cql, entity_type: this.opts.entity}),
      })
        .then(r => r.json())
        .then(d => {
          if (seq !== this.validateSeq) return;  // outdated
          if (d.valid) {
            this.statusEl.textContent = '✓ Фильтр корректен';
            this.statusEl.className = 'cql-tree-status text-success small';
            this.errorEl.textContent = '';
            if (this.opts.onValidity) this.opts.onValidity({ok: true});
          } else {
            this.statusEl.textContent = '✗ Ошибка';
            this.statusEl.className = 'cql-tree-status text-danger small';
            this.errorEl.textContent = d.error || '';
            if (this.opts.onValidity) this.opts.onValidity({ok: false, error: d.error});
          }
        })
        .catch(() => {});
    }

    _applyMode() {
      const ta = this.opts.textarea;
      const isText = this.mode === 'text';
      if (this.rootEl) this.rootEl.style.display = isText ? 'none' : '';
      if (ta) ta.style.display = isText ? '' : 'none';
      // Кнопки
      this.opts.container.querySelectorAll('[data-mode-btn]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.modeBtn === this.mode);
      });
      // Если builder отключён из-за parse-fail — кнопка builder становится disabled
      const builderBtn = this.opts.container.querySelector('[data-mode-btn="builder"]');
      if (builderBtn) builderBtn.disabled = this.fallbackMode;
    }

    // ── Public API ─────────────────────────────────────────────────────
    getCQL() {
      return serializeNode(this.tree);
    }

    async setCQL(cqlString) {
      const s = (cqlString || '').trim();
      if (!s) {
        this.tree = defaultTree();
        this.fallbackMode = false;
        this._renderTree();
        this._writeTextarea('');
        this._applyMode();
        return;
      }
      try {
        const r = await fetch('/api/v1/dashboard/cql/parse-tree/', {
          method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json', 'X-CSRFToken': this.csrftoken},
          body: JSON.stringify({query: s, entity_type: this.opts.entity}),
        });
        const data = await r.json();
        if (data.ok) {
          this.tree = this._normalizeTree(data.tree);
          this.fallbackMode = false;
          this._renderTree();
          this._writeTextarea(s);
          this._applyMode();
          this._validate();
        } else {
          this.fallbackMode = true;
          this.mode = 'text';
          this._writeTextarea(s);
          this.errorEl.textContent = 'Не удалось разобрать фильтр: ' + (data.error || '');
          this._applyMode();
        }
      } catch (e) {
        this.fallbackMode = true;
        this.mode = 'text';
        this._writeTextarea(s);
        this.errorEl.textContent = 'Ошибка соединения с сервером';
        this._applyMode();
      }
    }

    _normalizeTree(node) {
      // Добавляем id'шники нодам, пришедшим с бэка
      if (!node.id) node.id = uid();
      if (node.type === 'group' && node.children) {
        node.children = node.children.map(c => this._normalizeTree(c));
      }
      return node;
    }

    setEntity(entity) {
      this.opts.entity = entity;
      // Кэш подсказок и labelCache привязан к сущности — сбрасываем.
      this._suggestCache = null;
      this.labelCache = null;
      // Не пересоздаём дерево — поля могут совпадать; но если field больше нет,
      // он отрендерится как (unknown) — пользователь его исправит.
      this._renderTree();
      this._validate();
    }

    setDefs(defs) {
      this.opts.defs = defs;
      this._renderTree();
    }

    switchMode(mode) {
      if (mode === this.mode) return;
      if (mode === 'builder' && this.fallbackMode) return;
      if (mode === 'builder') {
        // Если в textarea что-то новое — пытаемся распарсить
        const ta = this.opts.textarea;
        if (ta && ta.value !== this.getCQL()) {
          this.setCQL(ta.value);
          return;
        }
      }
      this.mode = mode;
      this._applyMode();
    }

    destroy() {
      this.opts.container.innerHTML = '';
    }
  }

  window.CQLBuilder = {
    init(opts) {
      return new CQLBuilderInstance(opts);
    },
  };
})();
