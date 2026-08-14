import React, { useState, useEffect, useCallback } from 'react';
import './App.css';
import { listCases, getHealth } from './api';
import { colors, navButtonStyle } from './theme';
import Overview from './pages/Overview';
import CaseDetail from './pages/CaseDetail';
import GovernanceEvents from './pages/GovernanceEvents';
import AuditLog from './pages/AuditLog';
import Metrics from './pages/Metrics';

const NAV_ITEMS = [
  { key: 'overview', label: 'Overview', icon: OverviewIcon },
  { key: 'governance', label: 'Governance Events', icon: GovernanceIcon },
  { key: 'audit', label: 'Audit Log', icon: AuditIcon },
  { key: 'metrics', label: 'Metrics', icon: MetricsIcon },
];

function OverviewIcon(props) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" {...props}>
      <rect x="1.5" y="1.5" width="5.5" height="5.5" rx="1" stroke="currentColor" strokeWidth="1.4" />
      <rect x="9" y="1.5" width="5.5" height="5.5" rx="1" stroke="currentColor" strokeWidth="1.4" />
      <rect x="1.5" y="9" width="5.5" height="5.5" rx="1" stroke="currentColor" strokeWidth="1.4" />
      <rect x="9" y="9" width="5.5" height="5.5" rx="1" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  );
}
function GovernanceIcon(props) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" {...props}>
      <path d="M8 1.5 14.5 13H1.5L8 1.5Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M8 6.3v3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="8" cy="11.1" r="0.9" fill="currentColor" />
    </svg>
  );
}
function AuditIcon(props) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" {...props}>
      <path d="M4 1.5h5.5L12.5 4.5V14a0.5 0.5 0 0 1-0.5 0.5H4a0.5 0.5 0 0 1-0.5-0.5V2a0.5 0.5 0 0 1 0.5-0.5Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M5.5 7h5M5.5 9.4h5M5.5 11.8h3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}
function MetricsIcon(props) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" {...props}>
      <path d="M2 13.5V2M2 13.5h12" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <rect x="4.2" y="8.5" width="2" height="4" rx="0.4" fill="currentColor" />
      <rect x="7.5" y="5.5" width="2" height="7" rx="0.4" fill="currentColor" />
      <rect x="10.8" y="3" width="2" height="9.5" rx="0.4" fill="currentColor" />
    </svg>
  );
}

function App() {
  const [activeNav, setActiveNav] = useState('overview');
  const [selectedTraceId, setSelectedTraceId] = useState(null);
  const [previewTraceId, setPreviewTraceId] = useState(null);
  const [filter, setFilter] = useState('all');
  const [cases, setCases] = useState([]);
  const [loading, setLoading] = useState(true);
  const [apiHealthy, setApiHealthy] = useState(false);

  const loadCases = useCallback(async () => {
    try {
      const res = await listCases();
      setCases(res.data);
      setLoading(false);
    } catch (err) {
      console.error('Error loading cases:', err);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    getHealth().then(() => setApiHealthy(true)).catch(() => setApiHealthy(false));
    loadCases();
    const interval = setInterval(loadCases, 8000);
    return () => clearInterval(interval);
  }, [loadCases]);

  const goNav = (key) => {
    setActiveNav(key);
    setSelectedTraceId(null);
    setPreviewTraceId(null);
  };

  const pendingCount = cases.filter(
    (c) => c.status === 'pending_review' || c.status === 'manual_review'
  ).length;

  const showDetail = !!selectedTraceId;

  return (
    <div style={{
      display: 'flex', height: '100vh', width: '100%', background: colors.bg, color: colors.text,
      fontFamily: "'Space Grotesk',-apple-system,BlinkMacSystemFont,sans-serif", overflow: 'hidden',
    }}>
      <aside style={{
        width: 230, flexShrink: 0, background: colors.sidebarBg, borderRight: `1px solid ${colors.border}`,
        display: 'flex', flexDirection: 'column', padding: '20px 14px', boxSizing: 'border-box',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 8px 22px' }}>
          <div style={{
            width: 32, height: 32, borderRadius: 7, background: colors.navy, color: colors.navyText,
            display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: 15, flexShrink: 0,
          }}>C</div>
          <div>
            <div style={{ fontSize: 14, fontWeight: 700, letterSpacing: '-0.01em' }}>Customer Refund Service</div>
          </div>
        </div>

        <nav style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const active = !showDetail && activeNav === item.key;
            return (
              <button key={item.key} onClick={() => goNav(item.key)} style={navButtonStyle(active)}>
                <Icon style={{ flexShrink: 0 }} />
                <span style={{ flex: 1, textAlign: 'left' }}>{item.label}</span>
                {item.key === 'overview' && pendingCount > 0 && (
                  <span style={{
                    fontSize: 10.5, fontFamily: 'ui-monospace,monospace', background: 'oklch(0.55 0.15 80 / 0.16)',
                    color: 'oklch(0.42 0.15 80)', padding: '1px 6px', borderRadius: 10,
                  }}>{pendingCount}</span>
                )}
              </button>
            );
          })}
        </nav>

        <div style={{ marginTop: 'auto', padding: '14px 8px 4px', borderTop: `1px solid ${colors.border}` }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div style={{
              width: 7, height: 7, borderRadius: '50%',
              background: apiHealthy ? colors.good : colors.danger,
            }} />
            <div style={{ fontSize: 11, color: colors.textMuted }}>
              {apiHealthy ? 'All agents nominal' : 'API offline'}
            </div>
          </div>
          <div style={{ fontSize: 10.5, color: colors.textFainter, marginTop: 6, fontFamily: 'ui-monospace,monospace' }}>
            policy_version v1.0
          </div>
        </div>
      </aside>

      <div style={{ flex: 1, overflowY: 'auto', minWidth: 0 }}>
        {showDetail ? (
          <CaseDetail
            traceId={selectedTraceId}
            onBack={() => { setSelectedTraceId(null); setPreviewTraceId(null); }}
            onChanged={loadCases}
          />
        ) : (
          <>
            {activeNav === 'overview' && (
              <Overview
                cases={cases}
                loading={loading}
                filter={filter}
                setFilter={setFilter}
                previewTraceId={previewTraceId}
                setPreviewTraceId={setPreviewTraceId}
                onSelectCase={setSelectedTraceId}
                onGoNav={goNav}
              />
            )}
            {activeNav === 'governance' && (
              <GovernanceEvents cases={cases} loading={loading} onSelectCase={setSelectedTraceId} />
            )}
            {activeNav === 'audit' && <AuditLog />}
            {activeNav === 'metrics' && <Metrics />}
          </>
        )}
      </div>
    </div>
  );
}

export default App;
