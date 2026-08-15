import React from 'react';
import { colors } from '../theme';

export default function ApprovalTriggerEvidence({ approval, title = 'Review trigger evidence' }) {
  const trigger = approval?.trigger || {};
  const triggerType = trigger.type || approval?.triggering_event_type || 'unknown';
  const policyIds = trigger.policyIds || approval?.policyIds || [];
  const hasScore = trigger.score !== null
    && trigger.score !== undefined
    && trigger.score !== ''
    && Number.isFinite(Number(trigger.score));

  return (
    <div style={{ marginTop: 13, padding: 12, borderRadius: 8, border: `1px solid ${colors.border}`, background: 'oklch(0.99 0.004 90)' }}>
      <div style={{ fontSize: 10.5, color: colors.textFaint, textTransform: 'uppercase', letterSpacing: '.04em' }}>{title}</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 10, marginTop: 9 }}>
        <EvidenceField label="Type" value={humanize(triggerType)} />
        {approval?.triggering_event_id && <EvidenceField label="Event ID" value={approval.triggering_event_id} mono />}
        {triggerType === 'governance' && (
          <>
            <EvidenceField label="Category" value={trigger.category || 'Not categorized'} mono />
            <EvidenceField label="Action" value={humanize(trigger.action || 'unknown')} />
            <EvidenceField label="Score" value={hasScore ? Number(trigger.score).toFixed(2) : 'N/A'} mono />
          </>
        )}
        {triggerType === 'policy_review' && (
          <>
            <EvidenceField label="Review type" value={humanize(trigger.reviewType || 'manual review')} />
            <EvidenceField label="Policy IDs" value={policyIds.length ? policyIds.join(', ') : 'None recorded'} mono />
          </>
        )}
      </div>
      {trigger.detail && (
        <div style={{ marginTop: 10, fontSize: 12, color: colors.textMuted, lineHeight: 1.5, overflowWrap: 'anywhere' }}>
          {trigger.detail}
        </div>
      )}
    </div>
  );
}

function EvidenceField({ label, value, mono }) {
  return (
    <div>
      <div style={{ fontSize: 10, color: colors.textFainter }}>{label}</div>
      <div style={{ fontSize: 11.5, marginTop: 2, fontFamily: mono ? 'ui-monospace,monospace' : undefined, overflowWrap: 'anywhere' }}>
        {value ?? '-'}
      </div>
    </div>
  );
}

function humanize(value) {
  return String(value || 'unknown')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
