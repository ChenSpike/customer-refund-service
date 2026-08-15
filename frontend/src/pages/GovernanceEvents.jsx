import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { queryGovernanceEvents } from '../api';
import { colors, card, pageWrap, requestedMoney } from '../theme';

export default function GovernanceEvents({ cases = [], onSelectCase }) {
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);

  const caseByTrace = useMemo(
    () => new Map(cases.map((caseItem) => [caseItem.traceId, caseItem])),
    [cases],
  );

  const load = useCallback(async (quiet = false) => {
    if (quiet) setRefreshing(true);
    else setLoading(true);
    try {
      const response = await queryGovernanceEvents({ limit: 1000 });
      setEvents(Array.isArray(response.data) ? response.data : []);
      setError(null);
    } catch (requestError) {
      setError(requestError.response?.data?.detail || 'Unable to load persisted governance events.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(() => load(true), 8000);
    return () => clearInterval(interval);
  }, [load]);

  return (
    <div style={{ ...pageWrap, maxWidth: 1040 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 18, flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontSize: 22, fontWeight: 700 }}>Persisted Governance History</div>
          <div style={{ fontSize: 13, color: colors.textMuted, marginTop: 4, maxWidth: 760, lineHeight: 1.5 }}>
            Every persisted interceptor event in the demo database, newest first. Allow events are historical checks, not incidents;
            Policy Agent allow outcomes remain embedded in Policy handoffs when no governance finding row is emitted.
          </div>
          <div style={{ fontSize: 11.5, color: colors.textFainter, marginTop: 8 }}>
            {events.length} persisted event{events.length === 1 ? '' : 's'} loaded
          </div>
        </div>
        <button onClick={() => load(true)} disabled={loading || refreshing} style={secondaryButtonStyle}>
          {loading || refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {error && <div role="alert" style={errorStyle}>{error}</div>}
      {loading && events.length === 0 && (
        <div style={{ marginTop: 24, color: colors.textMuted, fontSize: 12.5 }}>Loading persisted events…</div>
      )}
      {!loading && !error && events.length === 0 && (
        <div style={{ ...card, marginTop: 24, fontSize: 12.5, color: colors.textFaint }}>
          No governance events are persisted in the demo database.
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 24 }}>
        {events.map((event) => {
          const caseItem = caseByTrace.get(event.trace_id);
          const action = String(event.interceptor_action || 'unknown').toLowerCase();
          const isAllowed = action === 'allow';
          const hasScore = event.trigger_score !== null
            && event.trigger_score !== undefined
            && event.trigger_score !== ''
            && Number.isFinite(Number(event.trigger_score));
          return (
            <article key={event.event_id} style={{ ...card, borderColor: isAllowed ? colors.border : 'oklch(0.55 0.19 25 / 0.35)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 12 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 9, flexWrap: 'wrap' }}>
                  <span style={actionBadgeStyle(isAllowed)}>{humanize(action)}</span>
                  <span style={riskBadgeStyle}>{event.owasp_category || 'No category'}</span>
                  <span style={{ fontSize: 13.5, fontWeight: 700 }}>{event.riskLabel || event.owasp_category || 'Governance event'}</span>
                </div>
                <div style={{ fontSize: 11.5, color: colors.textFainter, textAlign: 'right' }}>
                  {event.relativeTime || event.created_at || 'Time unavailable'}
                  <div style={{ fontFamily: 'ui-monospace,monospace', marginTop: 3, overflowWrap: 'anywhere' }}>{event.event_id}</div>
                </div>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(145px, 1fr))', gap: 10, marginTop: 14 }}>
                <Field label="Trace" value={event.trace_id} mono />
                <Field label="Agent" value={humanize(event.agent)} />
                <Field label="Trigger score" value={hasScore ? Number(event.trigger_score).toFixed(2) : 'N/A'} mono />
                <Field label="Recorded" value={event.created_at || '-'} mono />
              </div>

              {caseItem && (
                <div style={{ marginTop: 13, fontSize: 12.5, color: 'oklch(0.3 0.02 260)' }}>
                  {caseItem.customer} · {caseItem.reasonLabel} · requested{' '}
                  <span style={{ fontFamily: 'ui-monospace,monospace' }}>{requestedMoney(caseItem.amount, caseItem.currency)}</span>
                </div>
              )}
              {event.offending_content && (
                <div style={{ marginTop: 13, padding: 12, borderRadius: 8, background: 'oklch(0.5 0.19 25 / 0.06)', fontSize: 12, lineHeight: 1.5, overflowWrap: 'anywhere' }}>
                  <div style={{ color: colors.dangerText, fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '.04em', marginBottom: 5 }}>
                    Persisted evidence
                  </div>
                  {event.offending_content}
                </div>
              )}

              <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 14 }}>
                <button onClick={() => onSelectCase(event.trace_id)} style={secondaryButtonStyle}>View Case</button>
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}

function Field({ label, value, mono }) {
  return (
    <div>
      <div style={{ fontSize: 10.5, color: colors.textFaint }}>{label}</div>
      <div style={{ fontSize: 12, marginTop: 3, fontFamily: mono ? 'ui-monospace,monospace' : undefined, overflowWrap: 'anywhere' }}>
        {value ?? '-'}
      </div>
    </div>
  );
}

function humanize(value) {
  if (!value) return 'Unknown';
  return String(value)
    .replace(/_agent\d*$/i, ' Agent')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase())
    .trim();
}

const actionBadgeStyle = (isAllowed) => ({
  fontSize: 10.5,
  fontWeight: 700,
  padding: '4px 9px',
  borderRadius: 12,
  background: isAllowed ? 'oklch(0.55 0.14 150 / 0.14)' : 'oklch(0.5 0.19 25 / 0.1)',
  color: isAllowed ? colors.goodText : colors.dangerText,
});

const riskBadgeStyle = {
  fontSize: 10.5,
  fontFamily: 'ui-monospace,monospace',
  padding: '4px 9px',
  borderRadius: 6,
  background: 'oklch(0.5 0.13 250 / 0.09)',
  color: colors.accentText,
};

const secondaryButtonStyle = {
  flexShrink: 0,
  fontSize: 12,
  padding: '7px 14px',
  borderRadius: 7,
  border: `1px solid ${colors.borderStrong}`,
  background: colors.card,
  color: 'oklch(0.3 0.02 260)',
  cursor: 'pointer',
};

const errorStyle = {
  marginTop: 18,
  padding: 12,
  borderRadius: 8,
  background: 'oklch(0.5 0.19 25 / 0.08)',
  color: colors.dangerText,
  fontSize: 12.5,
};
