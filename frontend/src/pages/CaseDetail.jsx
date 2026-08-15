import React, { useState, useEffect, useCallback } from 'react';
import { getCase } from '../api';
import ApprovalResolutionForm from '../components/ApprovalResolutionForm';
import ApprovalTriggerEvidence from '../components/ApprovalTriggerEvidence';
import { colors, card, money, requestedMoney } from '../theme';
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
  const isExecutionFailed = detail.status === 'execution_failed';
  const isResolved = detail.status === 'auto_approved' || detail.status === 'followup_approved' || detail.status === 'human_approved' || detail.status === 'rejected';
  const pendingApproval = (detail.approvals || []).find((approval) => approval.status === 'pending');
  const resolvedApprovals = (detail.approvals || []).filter(
    (approval) => approval.status !== 'pending' || approval.resolved_at,
  );
  const rawGovernanceScore = detail.governance?.triggerScore;
  const hasGovernanceScore = rawGovernanceScore !== null
    && rawGovernanceScore !== undefined
    && rawGovernanceScore !== ''
    && Number.isFinite(Number(rawGovernanceScore));
  const governanceScore = hasGovernanceScore ? Number(rawGovernanceScore) : null;

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
            {detail.customer} · trace <span style={{ fontFamily: 'ui-monospace,monospace' }}>{detail.traceId}</span> · requested{' '}
            <span style={{ fontFamily: 'ui-monospace,monospace' }}>{requestedMoney(detail.amount, detail.currency)}</span>
          </div>
        </div>
      </div>

      <PipelineStrip pipeline={detail.pipeline} />

      {detail.resolutionLineage?.type === 'customer_followup' && (
        <CustomerFollowupLineage lineage={detail.resolutionLineage} />
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1.5fr 1fr', gap: 22, marginTop: 24, alignItems: 'start' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 18, minWidth: 0 }}>
          <div style={card}>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 14 }}>Customer Request</div>
            <div style={{ fontSize: 13.5, color: 'oklch(0.25 0.02 260)', lineHeight: 1.5, marginBottom: 14 }}>
              "{detail.request?.sanitizedText}"
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, fontSize: 12.5 }}>
              <Field label="Reason" value={detail.reasonLabel} />
              <Field label="Requested Amount" value={requestedMoney(detail.request?.requestedAmount, detail.currency)} mono />
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
            {detail.policy?.blockedBeforeEvaluation && (
              <div style={{
                marginBottom: 14, padding: '10px 12px', borderRadius: 8,
                border: '1px solid oklch(0.55 0.19 25 / 0.35)',
                background: 'oklch(0.5 0.19 25 / 0.07)', color: colors.dangerText,
                fontSize: 12.5, lineHeight: 1.45,
              }}>
                <strong>Not evaluated.</strong> Triage Governance blocked this case before it reached the Policy Agent.
              </div>
            )}
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

          {detail.customerResponse?.body && (
            <div style={card}>
              <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>Customer Response</div>
              {detail.customerResponse.subjectLine && (
                <div style={{ fontSize: 11.5, color: colors.textFaint, marginBottom: 10 }}>
                  Subject: {detail.customerResponse.subjectLine}
                </div>
              )}
              <div style={{ fontSize: 13, color: 'oklch(0.25 0.02 260)', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
                {detail.customerResponse.body}
              </div>
              <div style={{ marginTop: 12, fontSize: 11.5, color: colors.textFaint }}>
                Semantic checks: {responseChecksPassed(detail.customerResponse.contentChecks) ? 'passed' : 'attention required'}
              </div>
            </div>
          )}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 18, minWidth: 0 }}>
          <div style={card}>
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 14 }}>Governance</div>
            <div style={{ fontSize: 11, color: colors.textFaint, display: 'flex', justifyContent: 'space-between' }}>
              <span>Trigger Score</span>
              <span style={{ fontFamily: 'ui-monospace,monospace' }}>{hasGovernanceScore ? governanceScore.toFixed(2) : 'N/A'}</span>
            </div>
            <div style={{ height: 6, borderRadius: 4, background: 'oklch(0.92 0.006 90)', marginTop: 6, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${hasGovernanceScore ? Math.round(governanceScore * 100) : 0}%`, background: colors.danger }} />
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
                <>
                  <ApprovalTriggerEvidence approval={pendingApproval} />
                  <ApprovalResolutionForm
                    traceId={detail.traceId}
                    approval={pendingApproval}
                    requestedAmount={detail.amount}
                    onResolved={handleResolved}
                  />
                </>
              )}
            </div>
          )}
          {resolvedApprovals.length > 0 && (
            <div style={card}>
              <div style={{ fontSize: 13, fontWeight: 700 }}>Human Review History</div>
              <div style={{ fontSize: 12, color: colors.textMuted, marginTop: 4, lineHeight: 1.5 }}>
                Persisted reviewer decisions and the evidence that triggered each review.
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 14 }}>
                {resolvedApprovals.map((approval) => (
                  <ResolvedApprovalEvidence key={approval.approval_id} approval={approval} currency={detail.currency} />
                ))}
              </div>
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
          {isExecutionFailed && (
            <div role="alert" style={{ ...card, borderColor: 'oklch(0.55 0.15 80 / 0.5)', background: 'oklch(0.55 0.15 80 / 0.06)' }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: colors.warnText }}>Operational execution failed</div>
              <div style={{ fontSize: 12.5, color: colors.textMuted, marginTop: 7, lineHeight: 1.55 }}>
                The workflow did not complete its execution route. A successful refund is not recorded; inspect the persisted timeline and audit evidence before retrying.
              </div>
              {detail.refund?.status && (
                <div style={{ fontSize: 11.5, color: colors.textFaint, marginTop: 9 }}>
                  Refund transaction status: <span style={{ fontFamily: 'ui-monospace,monospace' }}>{detail.refund.status}</span>
                </div>
              )}
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
                        <> This was a <strong>partial refund</strong>. The customer originally requested {requestedMoney(detail.amount, detail.currency)}.</>
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

function ResolvedApprovalEvidence({ approval, currency }) {
  const notes = approval.notesPayload?.text || approval.notes || 'No reviewer notes recorded.';
  const hasAmount = approval.resolved_amount !== null
    && approval.resolved_amount !== undefined
    && approval.resolved_amount !== '';
  return (
    <section style={{ padding: 14, borderRadius: 9, border: `1px solid ${colors.border}` }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontSize: 12.5, fontWeight: 700 }}>{humanizeDecision(approval.decision || approval.status)}</div>
          <div style={{ fontFamily: 'ui-monospace,monospace', fontSize: 10.5, color: colors.textFainter, marginTop: 3, overflowWrap: 'anywhere' }}>
            {approval.approval_id}
          </div>
        </div>
        <span style={{ fontSize: 10.5, color: colors.textFaint }}>{formatTimestamp(approval.resolved_at)}</span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginTop: 12, fontSize: 12 }}>
        <Field label="Decision" value={humanizeDecision(approval.decision || approval.status)} />
        <Field label="Resolved Amount" value={hasAmount ? requestedMoney(approval.resolved_amount, currency) : 'Not applicable'} mono />
        <Field label="Reviewer" value={approval.reviewer || 'Not recorded'} mono />
        <Field label="Resolved At" value={formatTimestamp(approval.resolved_at)} mono />
      </div>
      <div style={{ marginTop: 11 }}>
        <div style={{ fontSize: 10.5, color: colors.textFaint }}>Reviewer notes</div>
        <div style={{ fontSize: 12, color: colors.textMuted, lineHeight: 1.5, marginTop: 3, whiteSpace: 'pre-wrap' }}>{notes}</div>
      </div>
      <ApprovalTriggerEvidence approval={approval} title="Original review trigger" />
    </section>
  );
}

function humanizeDecision(value) {
  return String(value || 'resolved')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatTimestamp(value) {
  if (!value) return 'Time not recorded';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
}

function responseChecksPassed(checks = {}) {
  const required = [
    'decision_reflected',
    'missing_info_requested',
    'safe_summary_reflected',
    'outcome_anchor_reflected',
  ];
  return required.every((name) => checks[name] === true)
    && Array.isArray(checks.pii_fields_detected)
    && checks.pii_fields_detected.length === 0
    && Array.isArray(checks.forbidden_phrases)
    && checks.forbidden_phrases.length === 0;
}

function CustomerFollowupLineage({ lineage }) {
  const initial = lineage.initial || {};
  const followup = lineage.followup || {};
  return (
    <div style={{ ...card, marginTop: 18, borderColor: 'oklch(0.55 0.12 220 / 0.45)' }}>
      <div style={{ fontSize: 13, fontWeight: 700 }}>Two-stage customer resolution</div>
      <div style={{ fontSize: 12, color: colors.textMuted, marginTop: 4 }}>
        This case first paused for missing information, then completed after the customer replied.
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 28px 1fr', gap: 12, alignItems: 'stretch', marginTop: 16 }}>
        <div style={{ padding: 14, borderRadius: 9, border: `1px solid ${colors.border}` }}>
          <StatusBadge status={initial.status || 'needs_info'} sourceAgent="Initial response" />
          <div style={{ fontSize: 11, color: colors.textFaint, marginTop: 12 }}>Original request-info response</div>
          <div style={{
            marginTop: 5, fontSize: 12.5, lineHeight: 1.5, color: 'oklch(0.3 0.02 260)',
            whiteSpace: 'pre-wrap', maxHeight: 180, overflowY: 'auto',
          }}>
            {initial.customerResponse?.body || 'The original request-info response is unavailable.'}
          </div>
        </div>
        <div aria-hidden="true" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', color: colors.textFaint }}>→</div>
        <div style={{ padding: 14, borderRadius: 9, border: '1px solid oklch(0.55 0.12 220 / 0.35)' }}>
          <StatusBadge status={followup.status || 'followup_approved'} sourceAgent="Customer follow-up" />
          <div style={{ fontSize: 12.5, color: 'oklch(0.3 0.02 260)', lineHeight: 1.5, marginTop: 12 }}>
            The customer supplied the requested facts. Policy re-evaluated the case and the refund completed without human review.
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
