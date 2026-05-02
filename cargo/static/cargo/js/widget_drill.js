/* widget_drill.js
 * Click-to-drill — строит CQL из координат клика на виджете и переходит
 * на универсальную страницу /drill/ с этим фильтром.
 *
 * Публичный API (глобальные функции):
 *   widgetDrillFromChart({widget, chartData, sliceIndex})
 *   widgetDrillFromPivot({widget, data, ri, ci, mi})
 *   widgetDrillFromStat ({widget})
 */
(function () {
  'use strict';

  const _MISSING = '__pivot_missing__';

  // ── CQL helpers ──────────────────────────────────────────────────────────
  function _quote(v) {
    if (v === null || v === undefined) return '""';
    const s = String(v);
    // Если значение уже численное и похоже на число — выводим без кавычек
    if (/^-?\d+(\.\d+)?$/.test(s)) return s;
    const esc = s.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    return `"${esc}"`;
  }

  function _cqlAtom(fieldKey, rawValue) {
    // Маркер «нет значения» → специальное сравнение.
    if (rawValue === _MISSING || rawValue === null || rawValue === '') {
      return `${fieldKey} IS NULL`;
    }
    // Булевы значения (строки 'True' / 'False' из tuple-ключей)
    if (rawValue === true || rawValue === 'True')  return `${fieldKey} = true`;
    if (rawValue === false || rawValue === 'False') return `${fieldKey} = false`;
    return `${fieldKey} = ${_quote(rawValue)}`;
  }

  function _combine(base, extra) {
    const parts = [];
    if (base && base.trim()) parts.push(`(${base.trim()})`);
    for (const e of extra) {
      if (e && e.trim()) parts.push(e.trim());
    }
    return parts.join(' AND ');
  }

  function _navigateToDrill(entity, cql, extra) {
    const params = new URLSearchParams();
    params.set('entity', entity);
    if (cql) params.set('cql', cql);
    if (extra && extra.widget_id)    params.set('widget_id', extra.widget_id);
    if (extra && extra.widget_title) params.set('widget_title', extra.widget_title);
    window.location.href = '/drill/?' + params.toString();
  }

  // ── Pivot drill ──────────────────────────────────────────────────────────
  function widgetDrillFromPivot(params) {
    const w    = params.widget;
    const data = params.data  || {};
    const ri   = params.ri | 0;
    const ci   = params.ci | 0;
    if (!w || !w.entity_type) return;

    const rows     = data.row_labels || [];
    const cols     = data.col_labels || [];
    const row      = rows[ri];
    const col      = cols[ci];
    if (!row) return;

    // Поддержка multi-dim и legacy single-dim.
    const rowByList = Array.isArray(data.row_by) ? data.row_by : (data.row_by ? [data.row_by] : []);
    const colByList = Array.isArray(data.col_by) ? data.col_by : (data.col_by ? [data.col_by] : []);
    const rowKeys   = Array.isArray(row.keys) ? row.keys : (row.key !== undefined ? [row.key] : []);
    const colKeys   = Array.isArray(col.keys) ? col.keys : (col && col.key !== undefined ? [col.key] : []);

    const atoms = [];
    for (let i = 0; i < rowByList.length && i < rowKeys.length; i++) {
      atoms.push(_cqlAtom(rowByList[i], rowKeys[i]));
    }
    for (let i = 0; i < colByList.length && i < colKeys.length; i++) {
      // Если colKey === '__all__' (single-dim без col_by), пропускаем
      if (colKeys[i] === '__all__') continue;
      atoms.push(_cqlAtom(colByList[i], colKeys[i]));
    }

    const cql = _combine(w.filter_query || '', atoms);
    _navigateToDrill(w.entity_type, cql, {
      widget_id:    w.id,
      widget_title: w.title || '',
    });
  }

  // ── Chart (bar / pie / warehouse) drill ──────────────────────────────────
  function widgetDrillFromChart(params) {
    const w         = params.widget;
    const chartData = params.chartData || {};
    const idx       = params.sliceIndex | 0;
    if (!w || !w.entity_type) return;

    // group_by может быть явно указан (chart_pie) или выведен по типу виджета
    const groupBy = chartData.group_by || _chartGroupBy(w.widget_type);
    if (!groupBy) {
      console.debug('drill: unknown group_by for widget_type', w.widget_type);
      return;
    }
    // group_keys предпочтительнее, чем labels (это raw-значения для CQL)
    const keys   = chartData.group_keys || chartData.labels || [];
    const rawVal = keys[idx];
    if (rawVal === undefined) return;

    const atom = _cqlAtom(groupBy, rawVal);
    const cql = _combine(w.filter_query || '', [atom]);
    _navigateToDrill(w.entity_type, cql, {
      widget_id:    w.id,
      widget_title: w.title || '',
    });
  }

  function _chartGroupBy(widget_type) {
    if (widget_type === 'chart_stage')     return 'stage';
    if (widget_type === 'chart_warehouse') return 'warehouse';
    return null;
  }

  // ── Stat widget drill ────────────────────────────────────────────────────
  function widgetDrillFromStat(params) {
    const w = params.widget;
    if (!w || !w.entity_type) return;
    // У простых stat-виджетов нет дополнительных координат — передаём только CQL виджета.
    _navigateToDrill(w.entity_type, (w.filter_query || '').trim(), {
      widget_id:    w.id,
      widget_title: w.title || '',
    });
  }

  // Экспорт
  window.widgetDrillFromPivot = widgetDrillFromPivot;
  window.widgetDrillFromChart = widgetDrillFromChart;
  window.widgetDrillFromStat  = widgetDrillFromStat;
})();
