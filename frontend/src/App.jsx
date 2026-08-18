import React, { useState, useEffect, useCallback } from 'react';
import './App.css';
import { getHealth, getPendingApprovals, listCases } from './api';
import { colors, navButtonStyle } from './theme';
import Overview from './pages/Overview';
import CaseDetail from './pages/CaseDetail';
import PendingApprovals from './pages/PendingApprovals';
import GovernanceEvents from './pages/GovernanceEvents';
import AuditLog from './pages/AuditLog';
import Metrics from './pages/Metrics';

const NAV_ITEMS = [
  { key: 'overview', label: 'Overview', icon: OverviewIcon },
  { key: 'approvals', label: 'Pending Approvals', icon: ApprovalIcon },
  { key: 'governance', label: 'Governance Events', icon: GovernanceIcon },
  { key: 'audit', label: 'Audit Log', icon: AuditIcon },
  { key: 'metrics', label: 'Metrics', icon: MetricsIcon },
];

const DEMO_TRACE = /^demo(?:0[1-9]|1[0-9]|20)$/;

function traceFromLocation() {
  if (typeof window === 'undefined') return null;
  const traceId = new URLSearchParams(window.location.search).get('trace');
  return DEMO_TRACE.test(traceId || '') ? traceId : null;
}

function replaceTraceQuery(traceId) {
  if (typeof window === 'undefined') return;
  const url = new URL(window.location.href);
  if (traceId) url.searchParams.set('trace', traceId);
  else url.searchParams.delete('trace');
  window.history.replaceState({}, '', url);
}

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
function ApprovalIcon(props) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" {...props}>
      <path d="M8 1.5 13 3.4v3.8c0 3.2-2 5.8-5 7.3-3-1.5-5-4.1-5-7.3V3.4L8 1.5Z" stroke="currentColor" strokeWidth="1.35" strokeLinejoin="round" />
      <path d="m5.4 8 1.55 1.55L10.8 5.7" stroke="currentColor" strokeWidth="1.35" strokeLinecap="round" strokeLinejoin="round" />
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

function SidebarToggleIcon({ collapsed, ...props }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" {...props}>
      <path d="M2.5 2.5h11v11h-11z" stroke="currentColor" strokeWidth="1.2" rx="1" />
      <path d="M6 2.5v11" stroke="currentColor" strokeWidth="1.2" />
      {collapsed ? (
        <path d="m9.2 8 2-2M9.2 8l2 2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      ) : (
        <path d="m10.8 8-2-2M10.8 8l-2 2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      )}
    </svg>
  );
}

function App() {
  const [activeNav, setActiveNav] = useState('overview');
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [selectedTraceId, setSelectedTraceId] = useState(traceFromLocation);
  const [previewTraceId, setPreviewTraceId] = useState(null);
  const [filter, setFilter] = useState('all');
  const [cases, setCases] = useState([]);
  const [loading, setLoading] = useState(true);
  const [caseError, setCaseError] = useState(null);
  const [health, setHealth] = useState({ status: 'checking' });
  const [pendingApprovalCount, setPendingApprovalCount] = useState(null);

  const loadCases = useCallback(async () => {
    try {
      const res = await listCases();
      setCases(res.data);
      setCaseError(null);
      setLoading(false);
    } catch (err) {
      console.error('Error loading cases:', err);
      setCaseError(err.response?.data?.detail || 'Unable to load workflow cases.');
      setLoading(false);
    }
  }, []);

  const loadPendingApprovalCount = useCallback(async () => {
    try {
      const response = await getPendingApprovals();
      setPendingApprovalCount(Array.isArray(response.data) ? response.data.length : 0);
    } catch (error) {
      console.error('Error loading pending approval count:', error);
      setPendingApprovalCount(null);
    }
  }, []);

  const loadHealth = useCallback(async () => {
    try {
      const response = await getHealth();
      setHealth(response.data);
    } catch (error) {
      setHealth(error.response?.data || { status: 'offline' });
    }
  }, []);

  const refreshDashboard = useCallback(async () => {
    await Promise.all([loadCases(), loadPendingApprovalCount(), loadHealth()]);
  }, [loadCases, loadPendingApprovalCount, loadHealth]);

  useEffect(() => {
    refreshDashboard();
    const interval = setInterval(refreshDashboard, 8000);
    return () => clearInterval(interval);
  }, [refreshDashboard]);

  useEffect(() => {
    const syncTraceFromUrl = () => {
      setSelectedTraceId(traceFromLocation());
      setPreviewTraceId(null);
    };
    window.addEventListener('popstate', syncTraceFromUrl);
    return () => window.removeEventListener('popstate', syncTraceFromUrl);
  }, []);

  const openCase = (traceId) => {
    if (!DEMO_TRACE.test(traceId || '')) return;
    setSelectedTraceId(traceId);
    setPreviewTraceId(null);
    replaceTraceQuery(traceId);
  };

  const closeCase = () => {
    setSelectedTraceId(null);
    setPreviewTraceId(null);
    replaceTraceQuery(null);
  };

  const goNav = (key) => {
    setActiveNav(key);
    closeCase();
  };

  const fallbackPendingCount = cases.filter(
    (caseItem) => caseItem.status === 'pending_review'
      || caseItem.status === 'manual_review'
      || caseItem.status === 'quarantined'
  ).length;
  const pendingCount = pendingApprovalCount ?? fallbackPendingCount;

  const showDetail = !!selectedTraceId;
  const sidebarWidth = isSidebarCollapsed ? 84 : 230;

  return (
    <div style={{
      display: 'flex', height: '100vh', width: '100%', background: colors.bg, color: colors.text,
      fontFamily: "'Space Grotesk',-apple-system,BlinkMacSystemFont,sans-serif", overflow: 'hidden',
    }}>
      <aside style={{
        width: sidebarWidth, flexShrink: 0, background: colors.sidebarBg, borderRight: `1px solid ${colors.border}`,
        display: 'flex', flexDirection: 'column', padding: '20px 14px', boxSizing: 'border-box',
        transition: 'width 180ms ease', position: 'relative',
      }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, padding: '6px 8px 22px',
          justifyContent: isSidebarCollapsed ? 'center' : 'flex-start',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
            <div style={{
              width: 32, height: 32, borderRadius: 7, background: colors.navy, color: colors.navyText,
              display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: 15, flexShrink: 0,
            }}>C</div>
            {!isSidebarCollapsed && (
              <div>
                <div style={{ fontSize: 14, fontWeight: 700, letterSpacing: '-0.01em' }}>Customer Refund Service</div>
              </div>
            )}
          </div>
        </div>

        <button
          type="button"
          onClick={() => setIsSidebarCollapsed((value) => !value)}
          title={isSidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-label={isSidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          style={{
            position: 'absolute',
            top: 18,
            right: -15,
            width: 30,
            height: 30,
            borderRadius: 999,
            border: `1px solid ${colors.border}`,
            background: colors.card,
            color: colors.textMuted,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            cursor: 'pointer',
            zIndex: 2,
            boxShadow: '0 4px 14px oklch(0.22 0.02 260 / 0.08)',
          }}
        >
          <SidebarToggleIcon collapsed={isSidebarCollapsed} />
        </button>

        <nav style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const active = !showDetail && activeNav === item.key;
            return (
              <button
                key={item.key}
                onClick={() => goNav(item.key)}
                title={isSidebarCollapsed ? item.label : undefined}
                aria-label={item.label}
                style={{
                  ...navButtonStyle(active),
                  justifyContent: isSidebarCollapsed ? 'center' : 'flex-start',
                  padding: isSidebarCollapsed ? '10px 0' : '9px 12px',
                  position: 'relative',
                }}
              >
                <Icon style={{ flexShrink: 0 }} />
                {!isSidebarCollapsed && <span style={{ flex: 1, textAlign: 'left' }}>{item.label}</span>}
                {item.key === 'approvals' && pendingCount > 0 && (
                  <span style={{
                    fontSize: 10.5, fontFamily: 'ui-monospace,monospace', background: 'oklch(0.55 0.15 80 / 0.16)',
                    color: 'oklch(0.42 0.15 80)', padding: '1px 6px', borderRadius: 10,
                    position: isSidebarCollapsed ? 'absolute' : 'static',
                    top: isSidebarCollapsed ? 6 : 'auto',
                    right: isSidebarCollapsed ? 8 : 'auto',
                  }}>{pendingCount}</span>
                )}
              </button>
            );
          })}
        </nav>

        <div style={{
          marginTop: 'auto', padding: '14px 8px 4px', borderTop: `1px solid ${colors.border}`,
          display: 'flex', flexDirection: 'column', alignItems: isSidebarCollapsed ? 'center' : 'stretch',
        }}>
          <div
            title={health.status === 'ok' ? 'Dashboard + database connected' : 'Dashboard database unavailable'}
            style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: isSidebarCollapsed ? 'center' : 'flex-start' }}
          >
            <div style={{
              width: 7, height: 7, borderRadius: '50%',
              background: health.status === 'ok' ? colors.good : colors.danger,
            }} />
            {!isSidebarCollapsed && (
              <div style={{ fontSize: 11, color: colors.textMuted }}>
                {health.status === 'ok' ? 'Dashboard + database connected' : 'Dashboard database unavailable'}
              </div>
            )}
          </div>
          {!isSidebarCollapsed && (
            <div style={{ fontSize: 10.5, color: colors.textFainter, marginTop: 6, fontFamily: 'ui-monospace,monospace' }}>
              policy_version v1.0
            </div>
          )}
        </div>
      </aside>

      <div style={{ flex: 1, overflowY: 'auto', minWidth: 0 }}>
        {caseError && (
          <div style={{
            margin: '18px 40px 0', padding: '10px 14px', borderRadius: 8,
            color: colors.dangerText, background: 'oklch(0.5 0.19 25 / 0.08)',
            border: '1px solid oklch(0.5 0.19 25 / 0.2)', fontSize: 12.5,
          }}>{caseError}</div>
        )}
        {showDetail ? (
          <CaseDetail
            traceId={selectedTraceId}
            onBack={closeCase}
            onChanged={refreshDashboard}
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
                onSelectCase={openCase}
                onGoNav={goNav}
              />
            )}
            {activeNav === 'approvals' && (
              <PendingApprovals
                onSelectCase={openCase}
                onChanged={refreshDashboard}
              />
            )}
            {activeNav === 'governance' && (
              <GovernanceEvents cases={cases} loading={loading} onSelectCase={openCase} />
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
