import React, { useCallback, useEffect, useState } from 'react';
import { getPendingApprovals } from '../api';
import ApprovalResolutionForm from '../components/ApprovalResolutionForm';
import { card, colors, money, pageWrap } from '../theme';

export default function PendingApprovals({ onSelectCase, onChanged }) {
  const [approvals, setApprovals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const response = await getPendingApprovals();
      setApprovals(response.data);
      setError(null);
    } catch (requestError) {
      setError(requestError.response?.data?.detail || 'Unable to load pending approvals.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, 8000);
    return () => clearInterval(interval);
  }, [load]);

  const handleResolved = async () => {
    await load();
    await onChanged?.();
  };

  return (
    <main style={pageWrap}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 18, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <div>
          <h1 style={{ fontSize: 22, margin: 0 }}>Pending Approvals</h1>
          <p style={{ fontSize: 13, color: colors.textMuted, margin: '8px 0 0', maxWidth: 720, lineHeight: 1.5 }}>
            Resolve a persisted demo review with an explicit reviewer identity. Each confirmed decision is delegated to the workflow lifecycle and immediately resumes the safe downstream route.
          </p>
        </div>
        <button onClick={load} disabled={loading} style={secondaryButtonStyle}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {error && <div role="alert" style={errorStyle}>{error}</div>}
      {!loading && !error && approvals.length === 0 && (
        <div style={{ ...card, marginTop: 22, color: colors.textMuted, fontSize: 13 }}>
          No approvals are waiting for human review.
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(330px, 1fr))', gap: 18, marginTop: 22 }}>
        {approvals.map((approval) => (
          <section key={approval.approval_id} style={{ ...card, minWidth: 0 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'flex-start' }}>
              <div>
                <div style={{ fontFamily: 'ui-monospace,monospace', fontSize: 14, fontWeight: 700 }}>
                  {approval.trace_id}
                </div>
                <div style={{ fontFamily: 'ui-monospace,monospace', fontSize: 10.5, color: colors.textFainter, marginTop: 4, wordBreak: 'break-all' }}>
                  {approval.approval_id}
                </div>
              </div>
              <span style={pendingBadgeStyle}>Pending</span>
            </div>

            <div style={{ marginTop: 15, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <Field label="Requested" value={`$${money(approval.amount_requested)}`} mono />
              <Field label="Trigger" value={approval.trigger?.type || approval.triggering_event_type} />
              <Field label="Stage" value={approval.review_stage || approval.trigger?.reviewType || '-'} />
              <Field label="Approve route" value={approval.approved_next_agent || '-'} mono />
            </div>
            {approval.reason && (
              <div style={{ marginTop: 13, fontSize: 12, color: colors.textMuted, lineHeight: 1.5 }}>
                {approval.reason}
              </div>
            )}

            <button onClick={() => onSelectCase(approval.trace_id)} style={{ ...secondaryButtonStyle, marginTop: 14, width: '100%' }}>
              Open full case
            </button>
            <ApprovalResolutionForm
              traceId={approval.trace_id}
              approval={approval}
              requestedAmount={approval.amount_requested}
              onResolved={handleResolved}
            />
          </section>
        ))}
      </div>
    </main>
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

const secondaryButtonStyle = {
  border: `1px solid ${colors.borderStrong}`,
  borderRadius: 7,
  padding: '7px 11px',
  background: colors.card,
  color: colors.textMuted,
  fontSize: 12,
  cursor: 'pointer',
};

const pendingBadgeStyle = {
  fontSize: 10.5,
  padding: '3px 8px',
  borderRadius: 12,
  background: 'oklch(0.55 0.15 80 / 0.14)',
  color: colors.warnText,
  flexShrink: 0,
};

const errorStyle = {
  marginTop: 18,
  padding: 12,
  borderRadius: 8,
  background: 'oklch(0.5 0.19 25 / 0.08)',
  color: colors.dangerText,
  fontSize: 12.5,
};
