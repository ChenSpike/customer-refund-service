import React, { useMemo, useState } from 'react';
import { resolveApproval } from '../api';
import { colors, money } from '../theme';

const DEMO_TRACE = /^demo(?:0[1-9]|1[0-9]|20)$/;

function apiErrorMessage(error) {
  const detail = error.response?.data?.detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || 'Invalid request').join('; ');
  }
  return typeof detail === 'string' ? detail : 'Approval could not be resolved.';
}

function amountIsValid(value) {
  return /^\d+(?:\.\d{1,2})?$/.test(value) && Number(value) > 0;
}

function numericAmount(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function amountSuggestion(decision, requested, remaining) {
  if (!['approve', 'partial_refund'].includes(decision) || remaining <= 0) return '';
  const ceiling = Math.min(remaining, requested > 0 ? requested : remaining);
  // When the refundable balance is already below the requested amount, that
  // balance is the natural partial-refund suggestion (demo07/demo08). Only
  // split the request when a reviewer deliberately chooses a partial refund
  // despite the full requested amount still being refundable.
  const candidate = decision === 'partial_refund' && ceiling >= requested
    ? ceiling / 2
    : ceiling;
  const cents = Math.floor((candidate + Number.EPSILON) * 100) / 100;
  return cents > 0 && (decision !== 'partial_refund' || cents < requested)
    ? cents.toFixed(2)
    : '';
}

export default function ApprovalResolutionForm({
  traceId,
  approval,
  requestedAmount,
  onResolved,
}) {
  const [decision, setDecision] = useState('');
  const [resolvedAmount, setResolvedAmount] = useState('');
  const [reviewer, setReviewer] = useState('');
  const [notes, setNotes] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  const approvalId = approval?.approval_id;
  const refundRoute = approval?.approved_next_agent === 'refund_agent';
  const amountRequired = decision === 'partial_refund' || (decision === 'approve' && refundRoute);
  const financials = useMemo(() => {
    const paid = numericAmount(approval?.amount_paid);
    const prior = numericAmount(approval?.prior_refund_total);
    const remaining = numericAmount(
      approval?.remaining_refundable,
      Math.max(0, paid - prior)
    );
    return {
      requested: numericAmount(
        approval?.requested_amount ?? approval?.amount_requested ?? requestedAmount,
        remaining
      ),
      paid,
      prior,
      remaining,
      currency: approval?.currency || 'USD',
    };
  }, [approval, requestedAmount]);
  const supportedTrace = DEMO_TRACE.test(traceId || '');

  const changeDecision = (event) => {
    const next = event.target.value;
    setDecision(next);
    setError(null);
    setResult(null);
    if (next === 'deny' || (next === 'approve' && !refundRoute)) {
      setResolvedAmount('');
    } else if (refundRoute) {
      setResolvedAmount(amountSuggestion(next, financials.requested, financials.remaining));
    }
  };

  const submit = async (event) => {
    event.preventDefault();
    setError(null);
    setResult(null);

    if (!supportedTrace || !approvalId) {
      setError('Only pending approvals for demo01 through demo20 can be resolved here.');
      return;
    }
    if (!decision) {
      setError('Choose a decision.');
      return;
    }
    if (decision === 'partial_refund' && !refundRoute) {
      setError('Partial refund is available only when approval routes to the Refund Agent.');
      return;
    }
    if (!reviewer.trim()) {
      setError('Enter the reviewer identity.');
      return;
    }
    if (!notes.trim()) {
      setError('Enter review notes.');
      return;
    }
    if (amountRequired && !amountIsValid(resolvedAmount)) {
      setError('Enter a positive refund amount with at most two decimal places.');
      return;
    }
    if (amountRequired && Number(resolvedAmount) > financials.remaining) {
      setError(
        `Refund amount cannot exceed the ${financials.currency} $${money(financials.remaining)} remaining refundable balance.`
      );
      return;
    }
    if (
      amountRequired
      && financials.requested > 0
      && Number(resolvedAmount) > financials.requested
    ) {
      setError(
        `Refund amount cannot exceed the ${financials.currency} $${money(financials.requested)} requested amount.`
      );
      return;
    }
    if (
      decision === 'partial_refund'
      && financials.requested > 0
      && Number(resolvedAmount) >= financials.requested
    ) {
      setError('Partial refund must be less than the requested amount.');
      return;
    }

    const amountSummary = amountRequired ? ` for $${money(resolvedAmount)}` : '';
    const confirmed = window.confirm(
      `Confirm ${decision.replace('_', ' ')}${amountSummary} for ${traceId}?\n\n` +
      `Reviewer: ${reviewer.trim()}\n` +
      'This records the decision and immediately continues the workflow.'
    );
    if (!confirmed) return;

    setSubmitting(true);
    try {
      const response = await resolveApproval(traceId, {
        approval_id: approvalId,
        decision,
        resolved_amount: amountRequired ? Number(resolvedAmount) : null,
        reviewer: reviewer.trim(),
        notes: notes.trim(),
      });
      setResult(response.data);
      await onResolved?.(response.data);
    } catch (requestError) {
      setError(apiErrorMessage(requestError));
      if ([409, 502].includes(requestError.response?.status)) {
        await onResolved?.(null);
      }
    } finally {
      setSubmitting(false);
    }
  };

  if (!supportedTrace) {
    return (
      <div style={noticeStyle}>
        Resolution is disabled because this trace is outside demo01 through demo20.
      </div>
    );
  }

  return (
    <form onSubmit={submit} style={{ marginTop: 16 }}>
      <div style={financialGridStyle} aria-label={`Refund limits for ${traceId}`}>
        <FinancialFact label="Requested" value={financials.requested} currency={financials.currency} />
        <FinancialFact label="Paid" value={financials.paid} currency={financials.currency} />
        <FinancialFact label="Prior refunds" value={financials.prior} currency={financials.currency} />
        <FinancialFact label="Remaining refundable" value={financials.remaining} currency={financials.currency} emphasis />
      </div>

      <div style={{ fontSize: 11, color: colors.textFaint, marginBottom: 5 }}>Decision</div>
      <select
        value={decision}
        onChange={changeDecision}
        disabled={submitting}
        style={inputStyle}
        aria-label={`Decision for ${traceId}`}
      >
        <option value="">Select a decision…</option>
        <option value="approve">Approve</option>
        {refundRoute && <option value="partial_refund">Approve partial refund</option>}
        <option value="deny">Deny</option>
      </select>

      {amountRequired && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 11, color: colors.textFaint, marginBottom: 5 }}>
            Resolved refund amount · maximum {financials.currency} ${money(
              Math.min(financials.requested || financials.remaining, financials.remaining)
            )}
          </div>
          <input
            type="text"
            inputMode="decimal"
            value={resolvedAmount}
            onChange={(event) => setResolvedAmount(event.target.value)}
            placeholder="0.00"
            disabled={submitting}
            style={inputStyle}
            aria-label={`Resolved amount for ${traceId}`}
          />
        </div>
      )}

      <div style={{ marginTop: 12 }}>
        <div style={{ fontSize: 11, color: colors.textFaint, marginBottom: 5 }}>Reviewer identity</div>
        <input
          type="text"
          value={reviewer}
          onChange={(event) => setReviewer(event.target.value)}
          placeholder="reviewer@example.com"
          maxLength={255}
          autoComplete="username"
          disabled={submitting}
          style={inputStyle}
          aria-label={`Reviewer for ${traceId}`}
        />
      </div>

      <div style={{ marginTop: 12 }}>
        <div style={{ fontSize: 11, color: colors.textFaint, marginBottom: 5 }}>Review notes</div>
        <textarea
          value={notes}
          onChange={(event) => setNotes(event.target.value)}
          placeholder="Explain the evidence and decision."
          maxLength={4000}
          rows={4}
          disabled={submitting}
          style={{ ...inputStyle, resize: 'vertical', lineHeight: 1.45 }}
          aria-label={`Review notes for ${traceId}`}
        />
      </div>

      {error && <div role="alert" style={errorStyle}>{error}</div>}
      {result && (
        <div role="status" style={successStyle}>
          Decision recorded. Continuation: {result.continuation_status || 'completed'}.
        </div>
      )}

      <button
        type="submit"
        disabled={submitting || !approvalId}
        style={{
          marginTop: 14,
          width: '100%',
          border: 'none',
          borderRadius: 8,
          padding: '9px 12px',
          background: submitting ? colors.borderStrong : colors.navy,
          color: colors.navyText,
          fontSize: 12.5,
          fontWeight: 700,
          cursor: submitting ? 'wait' : 'pointer',
        }}
      >
        {submitting ? 'Resolving and continuing…' : 'Review and confirm decision'}
      </button>
      <div style={{ fontSize: 10.5, color: colors.textFainter, lineHeight: 1.45, marginTop: 8 }}>
        You will see a final confirmation before the decision is persisted and the workflow resumes.
      </div>
    </form>
  );
}

function FinancialFact({ label, value, currency, emphasis }) {
  return (
    <div>
      <div style={{ fontSize: 10.5, color: colors.textFaint }}>{label}</div>
      <div style={{
        marginTop: 3,
        fontFamily: 'ui-monospace,monospace',
        fontSize: 12,
        fontWeight: emphasis ? 700 : 500,
        color: emphasis ? colors.accentText : colors.text,
      }}>
        {currency} ${money(value)}
      </div>
    </div>
  );
}

const inputStyle = {
  boxSizing: 'border-box',
  width: '100%',
  border: `1px solid ${colors.borderStrong}`,
  borderRadius: 7,
  padding: '8px 10px',
  background: colors.card,
  color: colors.text,
  font: 'inherit',
  fontSize: 12.5,
};

const financialGridStyle = {
  display: 'grid',
  gridTemplateColumns: '1fr 1fr',
  gap: 10,
  marginBottom: 14,
  padding: 12,
  border: `1px solid ${colors.borderFaint}`,
  borderRadius: 8,
  background: 'oklch(0.985 0.004 90)',
};

const noticeStyle = {
  marginTop: 14,
  padding: 10,
  borderRadius: 7,
  background: 'oklch(0.55 0.15 80 / 0.1)',
  color: colors.warnText,
  fontSize: 12,
};

const errorStyle = {
  marginTop: 12,
  padding: 10,
  borderRadius: 7,
  background: 'oklch(0.5 0.19 25 / 0.08)',
  color: colors.dangerText,
  fontSize: 12,
  lineHeight: 1.45,
};

const successStyle = {
  marginTop: 12,
  padding: 10,
  borderRadius: 7,
  background: 'oklch(0.5 0.15 150 / 0.08)',
  color: colors.goodText,
  fontSize: 12,
};
