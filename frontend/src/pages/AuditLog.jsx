import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { queryAuditLog } from '../api';
import { colors, pageWrap, filterTabStyle } from '../theme';

const CATEGORY_STYLE = {
  Governance: { color: 'oklch(0.45 0.19 25)', bg: 'oklch(0.5 0.19 25 / 0.1)' },
  Policy: { color: 'oklch(0.45 0.14 250)', bg: 'oklch(0.5 0.13 250 / 0.1)' },
  Refund: { color: 'oklch(0.4 0.14 150)', bg: 'oklch(0.55 0.14 150 / 0.14)' },
  Admin: { color: colors.navy, bg: 'oklch(0.9 0.008 90)' },
  Triage: { color: 'oklch(0.42 0.15 80)', bg: 'oklch(0.55 0.15 80 / 0.14)' },
  System: { color: colors.textMuted, bg: 'oklch(0.92 0.006 90)' },
};

const CATEGORIES = ['All', 'Governance', 'Policy', 'Triage', 'Refund', 'Admin', 'System'];

function CategoryBadge({ category }) {
  const style = CATEGORY_STYLE[category] || CATEGORY_STYLE.System;
  return (
    <span style={{
      fontSize: 10.5, fontWeight: 600, padding: '3px 9px', borderRadius: 20,
      background: style.bg, color: style.color, whiteSpace: 'nowrap',
    }}>{category}</span>
  );
}

export default function AuditLog() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [traceFilter, setTraceFilter] = useState('');
  const [category, setCategory] = useState('All');

  const load = useCallback(() => {
    setLoading(true);
    const params = { limit: 200 };
    if (traceFilter) params.trace_id = traceFilter;
    queryAuditLog(params)
      .then((res) => { setRows(res.data); setError(null); })
      .catch((err) => setError(err.response?.data?.detail || 'Unable to load audit events.'))
      .finally(() => setLoading(false));
  }, [traceFilter]);

  useEffect(() => { load(); }, [load]);

  const counts = useMemo(() => {
    const c = { All: rows.length };
    CATEGORIES.slice(1).forEach((cat) => { c[cat] = rows.filter((r) => r.category === cat).length; });
    return c;
  }, [rows]);

  const filteredRows = useMemo(
    () => (category === 'All' ? rows : rows.filter((r) => r.category === category)),
    [rows, category]
  );

  return (
    <div style={pageWrap}>
      <div style={{ fontSize: 22, fontWeight: 700 }}>Audit Log</div>
      <div style={{ fontSize: 13, color: colors.textMuted, marginTop: 4 }}>
        A plain-English trail of what every agent and admin did, in order
      </div>

      <div style={{ display: 'flex', gap: 8, margin: '20px 0 14px', flexWrap: 'wrap' }}>
        {CATEGORIES.map((cat) => (
          <button key={cat} onClick={() => setCategory(cat)} style={filterTabStyle(category === cat)}>
            {cat} <span style={{ opacity: 0.6 }}>{counts[cat] ?? 0}</span>
          </button>
        ))}
      </div>

      <input
        type="text"
        placeholder="Filter by trace ID…"
        value={traceFilter}
        onChange={(e) => setTraceFilter(e.target.value)}
        style={inputStyle}
      />

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 18 }}>
        {error && <div style={{ color: colors.dangerText, fontSize: 12.5 }}>{error}</div>}
        {loading && <div style={{ color: colors.textMuted, fontSize: 12.5 }}>Loading…</div>}
        {!loading && filteredRows.length === 0 && (
          <div style={{ color: colors.textMuted, fontSize: 12.5 }}>No matching events.</div>
        )}
        {filteredRows.map((row) => (
          <AuditRow key={row.log_id} row={row} />
        ))}
      </div>
    </div>
  );
}

function AuditRow({ row }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{
      border: `1px solid ${colors.borderFaint}`, borderRadius: 10, padding: '14px 16px', background: colors.card,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start', minWidth: 0 }}>
          <CategoryBadge category={row.category} />
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: 13, color: 'oklch(0.25 0.02 260)', lineHeight: 1.4 }}>{row.summary}</div>
            <div style={{ fontSize: 11.5, color: colors.textFaint, marginTop: 4 }}>
              {row.relativeTime} · {row.actor}
              {row.trace_id && (
                <> · <span style={{ fontFamily: 'ui-monospace,monospace', color: 'oklch(0.5 0.14 250)' }}>{row.trace_id}</span></>
              )}
            </div>
          </div>
        </div>
        {row.payload_json && (
          <button onClick={() => setOpen((o) => !o)} style={{
            flexShrink: 0, fontSize: 11, padding: '4px 10px', borderRadius: 6, border: `1px solid ${colors.borderStrong}`,
            background: 'transparent', color: colors.textMuted, cursor: 'pointer',
          }}>{open ? 'Hide details' : 'Details'}</button>
        )}
      </div>
      {open && row.payload_json && (
        <pre style={{
          marginTop: 12, fontSize: 10.5, background: 'oklch(0.99 0.004 90)', border: `1px solid ${colors.borderFaint}`,
          borderRadius: 6, padding: 10, overflowX: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        }}>{JSON.stringify(JSON.parse(row.payload_json), null, 2)}</pre>
      )}
    </div>
  );
}

const inputStyle = {
  fontSize: 12.5, padding: '8px 12px', borderRadius: 8, border: `1px solid ${colors.borderStrong}`,
  background: colors.card, color: colors.text, minWidth: 260,
};
