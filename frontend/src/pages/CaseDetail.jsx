import React, { useState, useEffect, useCallback } from 'react';
import { getCase } from '../api';
import ApprovalResolutionForm from '../components/ApprovalResolutionForm';
import { colors, card, money } from '../theme';
import { StatusBadge, RiskTag } from './Overview';

export default function CaseDetail({ traceId, onBack, onChanged }) {
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await getCase(traceId);
      setDetail(response.data);
      setError(null);
    } catch (requestError) {
      setError(requestError.response?.data?.detail || 'Failed to load case');
    } finally {
      setLoading(false);
    }
  }, [traceId]);

  useEffect(() => { load(); }, [load]);

  if (loading && !detail) {
    return <div style={{ padding: '28px 40px', color: colors.textMuted }}>Loading case…</div>;
  }
  if (error) {
    return (
      <div style={{ padding: '28px 40px' }}>
        <BackButton onBack={onBack} />
        <div style={{ marginTop: 18, color: 'oklch(0.45 0.19 25)' }}>{error}</div>
      </div>
    );
  }
  if (!detail) return null;

  const isReview = detail.status === 'pending_review' || detail.status === 'manual_review';
  const isQuarantine = detail.status === 'quarantined';
  const isNeedsInfo = detail.status === 'needs_info';
  const isResolved = detail.status === 'auto_approved' || detail.status === 'human_approved' || detail.status === 'rejected';
  const pendingApproval = (detail.approvals || []).find((approval) => approval.status === 'pending');

  const handleResolved = async () => {
    await load();
    await onChanged?.();
  };

  return (
    <div style={{ padding: '28px 40px 60px', maxWidth: 1360 }}>
      <BackButton onBack={onBack} />

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginTop: 18, gap: 24, flexWrap: 'wrap' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <div style={{ fontSize: 20, fontWeight: 700, fontFamily: 'ui-monospace,monospace' }}>{detail.id}</div>
            <StatusBadge status={detail.status} sourceAgent={detail.statusSource} />
            <RiskTag tag={detail.riskTag} />
          </div>
          <div style={{ fontSize: 13, color: colors.textMuted, marginTop: 8 }}>
            {detail.customer} · trace <span style={{ fontFamily: 'ui-monospace,monospace' }}>{detail.traceId}</span> ·{' '}
            <span style={{ fontFamily: 'ui-monospace,monospace' }}>${money(detail.amount)}</span> {detail.currency}
          </div>
        </div>
      </div>

      <PipelineStrip pipeline={detail.pipeline} />

      <div style={{ display: 'grid', gridTemplateColumns: '1.5fr 1fr', gap: 22, marginTop: 24, alignItems: 'start' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 18, minWidth: 0 }}>
          <div style={card}>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 14 }}>Customer Request</div>
            <div style={{ fontSize: 13.5, color: 'oklch(0.25 0.02 260)', lineHeight: 1.5, marginBottom: 14 }}>
              "{detail.request?.sanitizedText}"
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, fontSize: 12.5 }}>
              <Field label="Reason" value={detail.reasonLabel} />
              <Field label="Requested Amount" value={`$${money(detail.request?.requestedAmount)}`} mono />
            </div>
          </div>

          <div style={card}>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 14 }}>Order Facts</div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, fontSize: 12.5 }}>
              <Field label="Order ID" value={detail.order?.orderId} mono />
              <Field label="Product Type" value={detail.order?.productType} />
              <Field label="Purchase Date" value={detail.order?.purchaseDate} />
              <Field label="Item Status" value={detail.order?.itemStatus} />
              <Field label="Amount Paid" value={`$${money(detail.order?.amountPaid)}`} mono />
              <Field label="Prior Refund Total" value={`$${money(detail.order?.priorRefundTotal)}`} mono />
            </div>
          </div>

          <div style={card}>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 14 }}>Policy Evaluation</div>
            {(detail.policy?.matchedPolicies || []).map((pol, i) => (
              <div key={i} style={{ display: 'flex', gap: 10, padding: '10px 0', borderBottom: `1px solid ${colors.borderFaint}` }}>
                <div style={{ fontFamily: 'ui-monospace,monospace', fontSize: 11.5, color: 'oklch(0.45 0.14 250)', flexShrink: 0 }}>{pol.id}</div>
                <div style={{ fontSize: 12.5, color: 'oklch(0.3 0.02 260)' }}>
                  {pol.summary}
                  <div style={{ color: colors.textFaint, marginTop: 2, fontSize: 11.5 }}>{pol.effect}</div>
                </div>
              </div>
            ))}
            {detail.hasGaps && (detail.policy?.gaps || []).map((gap, i) => (
              <div key={i} style={{ display: 'flex', gap: 10, padding: '10px 0', borderBottom: `1px solid ${colors.borderFaint}` }}>
                <div style={{
                  fontSize: 11, padding: '2px 8px', borderRadius: 5, background: 'oklch(0.55 0.15 80 / 0.14)',
                  color: 'oklch(0.42 0.15 80)', flexShrink: 0, height: 'fit-content',
                }}>{gap.type}</div>
                <div style={{ fontSize: 12.5, color: 'oklch(0.3 0.02 260)' }}>{gap.detail}</div>
              </div>
            ))}
            <div style={{ marginTop: 14, paddingTop: 14, borderTop: `1px solid ${colors.border}`, display: 'flex', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap' }}>
              <div>
                <div style={{ fontSize: 11, color: colors.textFaint }}>Decision</div>
                <div style={{ fontSize: 14, fontWeight: 700, marginTop: 2 }}>{detail.policy?.decision?.type}</div>
                <div style={{ fontSize: 12, color: colors.textMuted, marginTop: 4, maxWidth: 320 }}>{detail.policy?.decision?.reasonText}</div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div style={{ fontSize: 11, color: colors.textFaint }}>Confidence</div>
                <div style={{ fontSize: 14, fontFamily: 'ui-monospace,monospace', marginTop: 2 }}>{detail.policy?.decision?.confidence}</div>
              </div>
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 18, minWidth: 0 }}>
          <div style={card}>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 14 }}>Governance</div>
            <div style={{ fontSize: 11, color: colors.textFaint, display: 'flex', justifyContent: 'space-between' }}>
              <span>Trigger Score</span><span style={{ fontFamily: 'ui-monospace,monospace' }}>{detail.governance?.triggerScore}</span>
            </div>
            <div style={{ height: 6, borderRadius: 4, background: 'oklch(0.92 0.006 90)', marginTop: 6, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${Math.round((detail.governance?.triggerScore || 0) * 100)}%`, background: colors.danger }} />
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 16, fontSize: 12.5 }}>
              <span style={{ color: colors.textFaint }}>Interceptor Action</span>
              <span style={{ textTransform: 'capitalize', fontFamily: 'ui-monospace,monospace' }}>{detail.governance?.action}</span>
            </div>
            {detail.hasFlags && (
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 12 }}>
                {detail.governance.flags.map((flag, i) => (
                  <span key={i} style={{
                    fontSize: 10.5, fontFamily: 'ui-monospace,monospace', padding: '3px 8px', borderRadius: 5,
                    background: 'oklch(0.5 0.19 25 / 0.1)', color: 'oklch(0.45 0.19 25)',
                  }}>{flag}</span>
                ))}
              </div>
            )}
            {detail.governance?.offendingText && (
              <div style={{ marginTop: 14, padding: 12, background: 'oklch(0.5 0.19 25 / 0.06)', border: '1px solid oklch(0.5 0.19 25 / 0.25)', borderRadius: 8 }}>
                <div style={{ fontSize: 10.5, color: 'oklch(0.42 0.19 25)', textTransform: 'uppercase', letterSpacing: '.04em', marginBottom: 6 }}>Offending Content</div>
                <div style={{ fontSize: 12, fontFamily: 'ui-monospace,monospace', color: 'oklch(0.35 0.19 25)', lineHeight: 1.5 }}>{detail.governance.offendingText}</div>
              </div>
            )}
            {detail.governance?.piiFlag && (
              <div style={{ marginTop: 14, padding: 12, background: 'oklch(0.5 0.19 25 / 0.06)', border: '1px solid oklch(0.5 0.19 25 / 0.25)', borderRadius: 8 }}>
                <div style={{ fontSize: 10.5, color: 'oklch(0.42 0.19 25)', textTransform: 'uppercase', letterSpacing: '.04em', marginBottom: 6 }}>
                  PII Detected: {detail.governance.piiFlag.field}
                </div>
                <div style={{ fontSize: 12, color: 'oklch(0.3 0.02 260)', lineHeight: 1.5 }}>{detail.governance.piiFlag.note}</div>
              </div>
            )}
          </div>

          {(isReview || isQuarantine) && (
            <div style={card}>
              <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>Human Review</div>
              <div style={{ fontSize: 12.5, color: colors.textMuted, lineHeight: 1.55 }}>
                {detail.pendingApprovalId
                  ? <>Pending approval <span style={{ fontFamily: 'ui-monospace,monospace' }}>{detail.pendingApprovalId}</span>.</>
                  : 'The workflow is still preparing its review record.'}
                {' '}A confirmed decision is recorded by the lifecycle service before the workflow resumes.
              </div>
              {pendingApproval && (
                <ApprovalResolutionForm
                  traceId={detail.traceId}
                  approval={pendingApproval}
                  requestedAmount={detail.amount}
                  onResolved={handleResolved}
                />
              )}
            </div>
          )}
          {isNeedsInfo && (
            <div style={card}>
              <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 6 }}>Waiting on Customer</div>
              <div style={{ fontSize: 12.5, color: colors.textMuted }}>
                More information was requested from the customer. No admin action needed until they respond.
              </div>
            </div>
          )}
          {isResolved && (
            <div style={card}>
              <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 6 }}>Resolved</div>
              {detail.refund ? (
                <div style={{ fontSize: 12.5, color: colors.textMuted }}>
                  {detail.refund.status === 'issued' ? (
                    <>
                      Refund of <strong style={{ color: 'oklch(0.3 0.02 260)' }}>${money(detail.refund.amount)}</strong> issued.
                      {detail.refund.isPartial && (
                        <> This was a <strong>partial refund</strong>. The customer originally requested ${money(detail.amount)}.</>
                      )}
                    </>
                  ) : (
                    <>Refund attempt {detail.refund.status}. No funds moved.</>
                  )}
                </div>
              ) : (
                <div style={{ fontSize: 12.5, color: colors.textMuted }}>No further action required.</div>
              )}
            </div>
          )}

          <div style={card}>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 14 }}>Case Timeline</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {(detail.notes || []).map((note, i) => (
                <div key={i} style={{ display: 'flex', gap: 10 }}>
                  <div style={{ width: 6, height: 6, borderRadius: '50%', background: 'oklch(0.7 0.01 90)', marginTop: 6, flexShrink: 0 }} />
                  <div>
                    <div style={{ fontSize: 12, color: colors.textFaint }}>{note.time} · {note.actor}</div>
                    <div style={{ fontSize: 12.5, color: 'oklch(0.3 0.02 260)', marginTop: 2 }}>{note.text}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function BackButton({ onBack }) {
  return (
    <button onClick={onBack} style={{
      background: 'none', border: `1px solid ${colors.borderStrong}`, color: 'oklch(0.45 0.02 260)',
      fontSize: 12.5, padding: '6px 12px', borderRadius: 7, cursor: 'pointer',
    }}>← Back to queue</button>
  );
}

function Field({ label, value, mono }) {
  return (
    <div>
      <div style={{ color: colors.textFaint }}>{label}</div>
      <div style={{ marginTop: 2, fontFamily: mono ? 'ui-monospace,monospace' : undefined }}>{value ?? '-'}</div>
    </div>
  );
}

function PipelineStrip({ pipeline = [] }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'flex-end', marginTop: 26, padding: '22px 24px', background: colors.card,
      border: `1px solid ${colors.border}`, borderRadius: 12, overflowX: 'auto',
    }}>
      {pipeline.map((node, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'flex-end' }}>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, minWidth: 100 }}>
            <div style={{ width: 13, height: 13, borderRadius: '50%', background: node.color }} />
            <div style={{ fontSize: 11, textAlign: 'center', color: colors.textFaint, lineHeight: 1.3 }}>{node.label}</div>
          </div>
          {node.hasNext && (
            <div style={{ width: 32, height: 2, background: node.lineColor, margin: '0 2px 22px' }} />
          )}
        </div>
      ))}
    </div>
  );
}
