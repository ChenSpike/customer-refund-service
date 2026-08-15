// Shared visual language for the Customer Refund Service dashboard, ported
// from the "Refund Ops Console" design mockup (oklch palette, Space Grotesk).

export const colors = {
  bg: 'oklch(1 0 0)',
  sidebarBg: 'oklch(1 0 0)',
  border: 'oklch(0.88 0.01 90)',
  borderStrong: 'oklch(0.85 0.01 90)',
  borderFaint: 'oklch(0.92 0.006 90)',
  card: 'oklch(1 0 0)',
  text: 'oklch(0.22 0.02 260)',
  textMuted: 'oklch(0.5 0.02 260)',
  textFaint: 'oklch(0.55 0.02 260)',
  textFainter: 'oklch(0.6 0.015 260)',
  navy: 'oklch(0.26 0.05 260)',
  navyText: 'oklch(0.98 0.005 260)',
  accent: 'oklch(0.5 0.13 250)',
  accentText: 'oklch(0.4 0.13 250)',
  danger: 'oklch(0.5 0.19 25)',
  dangerText: 'oklch(0.45 0.19 25)',
  warn: 'oklch(0.55 0.15 80)',
  warnText: 'oklch(0.42 0.15 80)',
  good: 'oklch(0.5 0.15 150)',
  goodText: 'oklch(0.4 0.14 150)',
};

export const STATUS_META = {
  auto_approved: { label: 'Auto-Approved', color: colors.goodText, bg: 'oklch(0.55 0.14 150 / 0.14)' },
  followup_approved: { label: 'Follow-up Approved', color: 'oklch(0.38 0.12 220)', bg: 'oklch(0.55 0.12 220 / 0.13)' },
  human_approved: { label: 'Human-Approved', color: 'oklch(0.38 0.12 190)', bg: 'oklch(0.52 0.12 190 / 0.14)' },
  pending_review: { label: 'Pending Review', color: 'oklch(0.35 0.02 260)', bg: 'oklch(0.9 0.008 90)' },
  manual_review: { label: 'Manual Review', color: colors.warnText, bg: colors.card, border: 'oklch(0.55 0.15 80 / 0.45)' },
  needs_info: { label: 'Needs Info', color: colors.accentText, bg: 'oklch(0.5 0.13 250 / 0.1)' },
  quarantined: { label: 'Quarantined', color: colors.navyText, bg: colors.navy },
  execution_failed: { label: 'Execution Failed', color: colors.warnText, bg: 'oklch(0.55 0.15 80 / 0.14)', border: 'oklch(0.55 0.15 80 / 0.45)' },
  rejected: { label: 'Rejected', color: colors.dangerText, bg: 'oklch(0.5 0.19 25 / 0.1)' },
};

// Solid per-status fill for charts (donut slices, etc.) — same identity as
// STATUS_META, just a solid hue instead of a text/badge-background pairing.
export const STATUS_CHART_COLOR = {
  auto_approved: colors.good,
  followup_approved: 'oklch(0.55 0.12 220)',
  human_approved: 'oklch(0.52 0.12 190)',
  pending_review: 'oklch(0.65 0.01 90)',
  manual_review: colors.warn,
  needs_info: colors.accent,
  quarantined: colors.navy,
  execution_failed: colors.warn,
  rejected: colors.danger,
};

export const NODE_STATE_COLOR = {
  done: colors.good,
  current: colors.warn,
  blocked: colors.danger,
  pending: colors.borderStrong,
};

export function money(n) {
  const num = Number(n);
  return (Number.isFinite(num) ? num : 0).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function requestedMoney(n, currency = '') {
  if (n === null || n === undefined || n === '') return 'Not provided';
  const num = Number(n);
  if (!Number.isFinite(num)) return 'Not provided';
  return `${currency ? `${currency} ` : ''}$${money(num)}`;
}

export function navButtonStyle(active) {
  return {
    display: 'flex',
    alignItems: 'center',
    gap: 9,
    padding: '9px 12px',
    borderRadius: 8,
    fontSize: 13,
    textAlign: 'left',
    border: 'none',
    cursor: 'pointer',
    background: active ? colors.navy : 'transparent',
    color: active ? colors.navyText : 'oklch(0.45 0.02 260)',
    width: '100%',
  };
}

export function filterTabStyle(active) {
  return {
    fontSize: 12.5,
    padding: '7px 14px',
    borderRadius: 20,
    border: `1px solid ${active ? colors.accent : colors.borderStrong}`,
    background: active ? 'oklch(0.5 0.13 250 / 0.1)' : 'transparent',
    color: active ? colors.accentText : colors.textMuted,
    cursor: 'pointer',
  };
}

export const card = {
  background: colors.card,
  border: `1px solid ${colors.border}`,
  borderRadius: 12,
  padding: 20,
};

export const pageWrap = { padding: '32px 40px 60px' };
