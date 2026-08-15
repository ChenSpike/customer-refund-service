import React, { useMemo } from 'react';
import { colors, card, pageWrap, STATUS_META, filterTabStyle, requestedMoney } from '../theme';

const FILTERS = [
  { key: 'all', label: 'All', hint: 'Every case in the queue, regardless of status.' },
  { key: 'pending', label: 'Pending Review', hint: 'Waiting on a human decision, including cases currently held by the governance layer.' },
  { key: 'needs_info', label: 'Needs Info', hint: 'The customer needs to provide more information (missing order ID, or the Policy Agent / a human reviewer asked for clarification) before the case can proceed.' },
  { key: 'auto_approved', label: 'Auto-Approved', hint: 'Refund approved and issued by the Policy Agent + Refund Agent without any human intervention.' },
  { key: 'followup_approved', label: 'Follow-up Approved', hint: 'Refund approved after the customer supplied the requested information; no human reviewer intervened.' },
  { key: 'human_approved', label: 'Human-Approved', hint: 'Refund approved and completed after an explicit human-review decision.' },
  { key: 'quarantined', label: 'Quarantined', hint: 'Blocked by the governance layer (PII leak, prompt injection, semantic drift, etc.) and held pending human confirmation.' },
  { key: 'execution_failed', label: 'Execution Failed', hint: 'The workflow reached an operational failure and requires investigation or a safe retry.' },
  { key: 'rejected', label: 'Rejected', hint: 'Refund denied, either by the Policy Agent’s rule evaluation or a human reviewer.' },
];

function StatusBadge({ status, sourceAgent }) {
  const meta = STATUS_META[status] || STATUS_META.pending_review;
  return (
    <span
      title={sourceAgent ? `${meta.label} (by ${sourceAgent})` : meta.label}
      style={{
        fontSize: 11, padding: '4px 9px', borderRadius: 20, background: meta.bg, color: meta.color, fontWeight: 600,
        border: meta.border ? `1px solid ${meta.border}` : 'none', cursor: sourceAgent ? 'help' : 'default',
      }}
    >{meta.label}</span>
  );
}

function RiskTag({ tag, small }) {
  if (!tag) return null;
  return (
    <span style={{
      fontSize: small ? 10.5 : 11, fontFamily: 'ui-monospace,monospace', padding: small ? '3px 7px' : '4px 10px',
      borderRadius: small ? 5 : 6, background: 'oklch(0.5 0.19 25 / 0.1)', color: 'oklch(0.45 0.19 25)',
    }}>{tag.code}</span>
  );
}

export default function Overview({ cases, loading, filter, setFilter, previewTraceId, setPreviewTraceId, onSelectCase, onGoNav }) {
  const counts = useMemo(() => ({
    all: cases.length,
    auto_approved: cases.filter((c) => c.status === 'auto_approved').length,
    followup_approved: cases.filter((c) => c.status === 'followup_approved').length,
    human_approved: cases.filter((c) => c.status === 'human_approved').length,
    pending: cases.filter((c) => c.status === 'pending_review' || c.status === 'manual_review' || c.status === 'quarantined').length,
    needs_info: cases.filter((c) => c.status === 'needs_info').length,
    quarantined: cases.filter((c) => c.status === 'quarantined').length,
    execution_failed: cases.filter((c) => c.status === 'execution_failed').length,
    rejected: cases.filter((c) => c.status === 'rejected').length,
  }), [cases]);

  const filteredCases = useMemo(() => {
    if (filter === 'all') return cases;
    if (filter === 'pending') return cases.filter((c) => c.status === 'pending_review' || c.status === 'manual_review' || c.status === 'quarantined');
    return cases.filter((c) => c.status === filter);
  }, [cases, filter]);

  const incidents = useMemo(() => cases.filter((c) => c.riskTag).slice(0, 3), [cases]);
  const previewCase = cases.find((c) => c.traceId === previewTraceId) || null;
  const noHumanCompleted = cases.filter((c) => c.completedWithoutHuman).length;

  const overviewStats = [
    { label: 'Open Cases', value: String(counts.all - counts.auto_approved - counts.followup_approved - counts.human_approved - counts.rejected), detail: 'Currently moving through the pipeline' },
    { label: 'Pending Review', value: String(counts.pending), detail: 'Awaiting human sign-off, including governance holds' },
    { label: 'Quarantined', value: String(counts.quarantined), detail: 'Blocked by the governance layer' },
    { label: 'Auto-Approved', value: String(counts.auto_approved), detail: 'Resolved from the initial request' },
    { label: 'Follow-up Approved', value: String(counts.followup_approved), detail: 'Resolved after a customer reply' },
    { label: 'No-Human Completion', value: counts.all ? `${Math.round((noHumanCompleted / counts.all) * 100)}%` : '0%', detail: 'Completed approvals and denials without a reviewer' },
  ];

  if (loading) {
    return <div style={{ ...pageWrap, color: colors.textMuted }}>Loading cases…</div>;
  }

  return (
    <div style={{ ...pageWrap, maxWidth: 1180 }}>
      <div style={{ fontSize: 34, fontWeight: 700, letterSpacing: '-0.01em' }}>Overview</div>
      <div style={{ fontSize: 13.5, color: colors.textMuted, marginTop: 6, maxWidth: 640, lineHeight: 1.5 }}>
        Refund pipeline health at a glance: automated throughput, cases waiting on a human, and governance holds.
      </div>

      <div style={{ display: 'flex', marginTop: 28, borderTop: `1px solid ${colors.borderStrong}`, borderBottom: `1px solid ${colors.borderStrong}` }}>
        {overviewStats.map((st, i) => (
          <div key={st.label} style={{ flex: 1, padding: '18px 20px', borderLeft: i === 0 ? 'none' : `1px solid ${colors.border}` }}>
            <div style={{ fontSize: 10.5, color: 'oklch(0.5 0.14 250)', textTransform: 'uppercase', letterSpacing: '.05em', fontWeight: 600 }}>{st.label}</div>
            <div style={{ fontSize: 32, fontWeight: 700, marginTop: 8 }}>{st.value}</div>
            <div style={{ fontSize: 11.5, color: colors.textMuted, marginTop: 8, lineHeight: 1.4 }}>{st.detail}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginTop: 36 }}>
        <div style={{ fontSize: 19, fontWeight: 700 }}>Case Queue</div>
      </div>
      <div style={{ display: 'flex', gap: 8, margin: '14px 0', flexWrap: 'wrap' }}>
        {FILTERS.map((tab) => (
          <button key={tab.key} title={tab.hint} onClick={() => setFilter(tab.key)} style={filterTabStyle(filter === tab.key)}>
            {tab.label} <span style={{ opacity: 0.6 }}>{counts[tab.key]}</span>
          </button>
        ))}
      </div>

      <div style={{ border: `1px solid ${colors.border}`, borderRadius: 10, overflowX: 'auto' }}>
        <div style={{
          display: 'grid', gridTemplateColumns: '100px 1.4fr 96px 90px 128px 110px 70px', minWidth: 900,
          padding: '10px 16px', fontSize: 10.5, color: colors.textFaint, textTransform: 'uppercase',
          letterSpacing: '.04em', background: 'oklch(0.99 0.004 90)', borderBottom: `1px solid ${colors.border}`,
        }}>
          <div>Ticket</div><div>Customer / Request</div><div>Reason</div><div>Requested</div><div>Status</div><div>Risk</div><div>Updated</div>
        </div>
        {filteredCases.length === 0 && (
          <div style={{ padding: 20, fontSize: 12.5, color: colors.textMuted }}>No cases match this filter.</div>
        )}
        {filteredCases.map((c) => (
          <div
            key={c.traceId}
            onClick={() => setPreviewTraceId(previewTraceId === c.traceId ? null : c.traceId)}
            style={{
              display: 'grid', gridTemplateColumns: '100px 1.4fr 96px 90px 128px 110px 70px', minWidth: 900,
              padding: '14px 16px', borderBottom: `1px solid ${colors.borderFaint}`, cursor: 'pointer', alignItems: 'center',
            }}
          >
            <div style={{ fontFamily: 'ui-monospace,monospace', fontSize: 12 }}>{c.id}</div>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 13.5 }}>{c.customer}</div>
              <div style={{ fontSize: 11.5, color: colors.textFaint, marginTop: 2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.summary}</div>
            </div>
            <div style={{ fontSize: 12.5, color: 'oklch(0.4 0.02 260)' }}>{c.reasonLabel}</div>
            <div style={{ fontSize: 13, fontFamily: 'ui-monospace,monospace' }}>{requestedMoney(c.amount)}</div>
            <div><StatusBadge status={c.status} sourceAgent={c.statusSource} /></div>
            <div><RiskTag tag={c.riskTag} small /></div>
            <div style={{ fontSize: 11.5, color: colors.textFainter }}>{c.updated}</div>
          </div>
        ))}
      </div>

      {previewCase && (
        <div
          onClick={() => setPreviewTraceId(null)}
          style={{
            position: 'fixed', inset: 0, background: 'oklch(0.15 0.02 260 / 0.45)', display: 'flex',
            alignItems: 'center', justifyContent: 'center', zIndex: 50, padding: 24,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: colors.card, border: `1px solid ${colors.accent}`, borderRadius: 12, padding: 24,
              maxWidth: 620, width: '100%', boxShadow: '0 20px 60px oklch(0.15 0.02 260 / 0.25)',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 16 }}>
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                  <div style={{ fontSize: 16, fontWeight: 700, fontFamily: 'ui-monospace,monospace' }}>{previewCase.id}</div>
                  <StatusBadge status={previewCase.status} sourceAgent={previewCase.statusSource} />
                  <RiskTag tag={previewCase.riskTag} />
                </div>
                <div style={{ fontSize: 12.5, color: colors.textMuted, marginTop: 8 }}>
                  {previewCase.customer} · {previewCase.reasonLabel} · requested <span style={{ fontFamily: 'ui-monospace,monospace' }}>{requestedMoney(previewCase.amount)}</span>
                </div>
                <div style={{ fontSize: 13, color: 'oklch(0.3 0.02 260)', marginTop: 10 }}>
                  "{previewCase.request?.sanitizedText}"
                </div>
              </div>
              <button onClick={() => setPreviewTraceId(null)} aria-label="Close" style={{
                border: 'none', background: 'transparent', color: colors.textFaint, fontSize: 18, cursor: 'pointer',
                lineHeight: 1, padding: 4, flexShrink: 0,
              }}>×</button>
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 20, justifyContent: 'flex-end' }}>
              <button onClick={() => setPreviewTraceId(null)} style={{
                padding: '9px 16px', borderRadius: 8, border: `1px solid ${colors.borderStrong}`, background: 'transparent',
                color: colors.textMuted, fontSize: 12.5, cursor: 'pointer',
              }}>Close</button>
              <button onClick={() => onSelectCase(previewCase.traceId)} style={{
                padding: '9px 16px', borderRadius: 8, border: 'none', background: colors.navy, color: colors.navyText,
                fontSize: 12.5, fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap',
              }}>View Full Case →</button>
            </div>
          </div>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginTop: 32 }}>
        <button onClick={() => onGoNav('governance')} style={quickLinkStyle}>
          <div style={{ fontSize: 13.5, fontWeight: 700 }}>Go to Governance Feed →</div>
          <div style={{ fontSize: 12, color: colors.textMuted, marginTop: 4 }}>Inspect every persisted governance action</div>
        </button>
        <button onClick={() => onGoNav('audit')} style={quickLinkStyle}>
          <div style={{ fontSize: 13.5, fontWeight: 700 }}>Go to Audit Log →</div>
          <div style={{ fontSize: 12, color: colors.textMuted, marginTop: 4 }}>Full execution trail, every state</div>
        </button>
        <button onClick={() => onGoNav('metrics')} style={{ ...quickLinkStyle, gridColumn: 'span 2' }}>
          <div style={{ fontSize: 13.5, fontWeight: 700 }}>Go to Metrics →</div>
          <div style={{ fontSize: 12, color: colors.textMuted, marginTop: 4 }}>Cost, time, and quality KPIs</div>
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 22, marginTop: 22, alignItems: 'start' }}>
        <div style={card}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 14 }}>Active Governance Incidents</div>
          {incidents.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {incidents.map((inc) => (
                <div key={inc.traceId} onClick={() => onSelectCase(inc.traceId)} style={{
                  display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10,
                  padding: '10px 0', borderBottom: `1px solid ${colors.borderFaint}`, cursor: 'pointer',
                }}>
                  <div>
                    <RiskTag tag={inc.riskTag} />
                    <span style={{ fontSize: 12.5, marginLeft: 8 }}>{inc.riskTag.label}</span>
                  </div>
                  <div style={{ fontSize: 11.5, color: colors.textFaint }}>{inc.updated}</div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ fontSize: 12.5, color: colors.textFaint }}>No active incidents.</div>
          )}
        </div>

        <div style={card}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 14 }}>Recent Cases</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {cases.slice(0, 6).map((c) => (
              <div key={c.traceId} style={{ display: 'flex', gap: 10 }}>
                <div style={{ width: 6, height: 6, borderRadius: '50%', background: 'oklch(0.7 0.01 90)', marginTop: 6, flexShrink: 0 }} />
                <div>
                  <div style={{ fontSize: 11.5, color: colors.textFaint }}>{c.updated} · {c.customer}</div>
                  <div style={{ fontSize: 12.5, color: 'oklch(0.3 0.02 260)', marginTop: 2 }}>{c.reasonLabel} · {STATUS_META[c.status]?.label}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div style={{ fontSize: 11.5, color: colors.textFainter, marginTop: 28 }}>
        {cases.length} case{cases.length === 1 ? '' : 's'} loaded from the refund workflow database.
      </div>
    </div>
  );
}

const quickLinkStyle = {
  textAlign: 'left', padding: '16px 18px', borderRadius: 12, border: `1px solid ${colors.border}`,
  background: colors.card, color: colors.text, cursor: 'pointer',
};

export { StatusBadge, RiskTag };
