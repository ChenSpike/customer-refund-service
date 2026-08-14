import React, { useMemo } from 'react';
import { colors, card, pageWrap } from '../theme';

export default function GovernanceEvents({ cases, loading, onSelectCase }) {
  const incidents = useMemo(() => cases.filter((c) => c.riskTag), [cases]);

  return (
    <div style={{ ...pageWrap, maxWidth: 980 }}>
      <div style={{ fontSize: 22, fontWeight: 700 }}>Governance Feed</div>
      <div style={{ fontSize: 13, color: colors.textMuted, marginTop: 4 }}>
        Interceptor actions at every agent handoff, mapped to OWASP Agentic AI risk categories
      </div>

      {loading && <div style={{ marginTop: 24, color: colors.textMuted, fontSize: 12.5 }}>Loading…</div>}

      {!loading && incidents.length === 0 && (
        <div style={{ marginTop: 24, fontSize: 12.5, color: colors.textFaint }}>No governance incidents recorded.</div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 24 }}>
        {incidents.map((inc) => (
          <div key={inc.traceId} style={card}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <span style={{
                  fontSize: 11, fontFamily: 'ui-monospace,monospace', padding: '4px 10px', borderRadius: 6,
                  background: 'oklch(0.5 0.19 25 / 0.1)', color: 'oklch(0.45 0.19 25)',
                }}>{inc.riskTag.code}</span>
                <span style={{ fontSize: 13.5, fontWeight: 700 }}>{inc.riskTag.label}</span>
              </div>
              <div style={{ fontSize: 11.5, color: colors.textFainter }}>
                {inc.updated} · <span style={{ fontFamily: 'ui-monospace,monospace' }}>{inc.id}</span>
              </div>
            </div>
            <div style={{ marginTop: 12, fontSize: 12.5, color: 'oklch(0.3 0.02 260)' }}>
              {inc.customer} · {inc.reasonLabel} · <span style={{ fontFamily: 'ui-monospace,monospace' }}>${Number(inc.amount).toFixed(2)}</span>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', marginTop: 14, gap: 16 }}>
              <div style={{ fontSize: 12.5, color: colors.textMuted, maxWidth: 500 }}>{inc.summary}</div>
              <button onClick={() => onSelectCase(inc.traceId)} style={{
                flexShrink: 0, fontSize: 12, padding: '7px 14px', borderRadius: 7, border: `1px solid ${colors.borderStrong}`,
                background: 'transparent', color: 'oklch(0.3 0.02 260)', cursor: 'pointer',
              }}>View Case</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
