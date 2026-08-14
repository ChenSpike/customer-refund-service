import React, { useState, useEffect } from 'react';
import {
  ResponsiveContainer, PieChart, Pie, Cell, Tooltip,
  BarChart, Bar, XAxis, YAxis, CartesianGrid, LabelList,
} from 'recharts';
import { getConsoleMetrics } from '../api';
import { colors, card, pageWrap, STATUS_META, STATUS_CHART_COLOR } from '../theme';

export default function Metrics() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getConsoleMetrics()
      .then((res) => setData(res.data))
      .catch((err) => console.error('Error loading metrics:', err))
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return <div style={{ ...pageWrap, color: colors.textMuted }}>Loading metrics…</div>;
  }
  if (!data) {
    return <div style={{ ...pageWrap, color: colors.textMuted }}>No metrics available.</div>;
  }

  const statusData = (data.statusBreakdown || []).map((d) => ({
    ...d,
    label: STATUS_META[d.status]?.label || d.status,
  }));
  const totalCases = statusData.reduce((sum, d) => sum + d.count, 0);
  const owaspData = data.owaspBreakdown || [];

  return (
    <div style={{ ...pageWrap, maxWidth: 1180 }}>
      <div style={{ fontSize: 22, fontWeight: 700 }}>Metrics</div>
      <div style={{ fontSize: 13, color: colors.textMuted, marginTop: 4 }}>
        Cost, time, and quality KPIs computed from live case data
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 14, marginTop: 24 }}>
        {data.primaryStats.map((st) => (
          <div key={st.label} style={{ background: colors.card, border: `1px solid ${colors.border}`, borderRadius: 12, padding: 18 }}>
            <div style={{ fontSize: 11, color: colors.textFaint }}>{st.label}</div>
            <div style={{ fontSize: 22, fontWeight: 700, marginTop: 6 }}>{st.value}</div>
            <div style={{ fontSize: 11.5, color: colors.textMuted, marginTop: 8, lineHeight: 1.4 }}>{st.detail}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 14, marginTop: 14 }}>
        {data.secondaryStats.map((st) => (
          <div key={st.label} style={{ background: 'oklch(0.99 0.004 90)', border: `1px solid ${colors.border}`, borderRadius: 12, padding: 16 }}>
            <div style={{ fontSize: 18, fontWeight: 700, fontFamily: 'ui-monospace,monospace' }}>{st.value}</div>
            <div style={{ fontSize: 11.5, color: colors.textMuted, marginTop: 4 }}>{st.label}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginTop: 22, alignItems: 'start' }}>
        <div style={card}>
          <div style={{ fontSize: 13, fontWeight: 700 }}>Case Status Breakdown</div>
          <div style={{ fontSize: 11.5, color: colors.textMuted, marginTop: 2, marginBottom: 8 }}>
            {totalCases} case{totalCases === 1 ? '' : 's'} in the queue
          </div>
          {totalCases === 0 ? (
            <div style={{ fontSize: 12.5, color: colors.textFaint, padding: '24px 0' }}>No cases yet.</div>
          ) : (
            <>
              <ResponsiveContainer width="100%" height={200}>
                <PieChart>
                  <Pie
                    data={statusData} dataKey="count" nameKey="label"
                    innerRadius={52} outerRadius={82} paddingAngle={2}
                    stroke={colors.card} strokeWidth={2}
                  >
                    {statusData.map((d) => <Cell key={d.status} fill={STATUS_CHART_COLOR[d.status]} />)}
                  </Pie>
                  <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8, border: `1px solid ${colors.border}` }} />
                </PieChart>
              </ResponsiveContainer>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 7, marginTop: 6 }}>
                {statusData.map((d) => (
                  <div key={d.status} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
                    <span style={{ width: 9, height: 9, borderRadius: 3, background: STATUS_CHART_COLOR[d.status], flexShrink: 0 }} />
                    <span style={{ flex: 1, color: colors.textMuted }}>{d.label}</span>
                    <span style={{ fontFamily: 'ui-monospace,monospace', fontWeight: 600 }}>{d.count}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>

        <div style={card}>
          <div style={{ fontSize: 13, fontWeight: 700 }}>Governance Holds by OWASP Category</div>
          <div style={{ fontSize: 11.5, color: colors.textMuted, marginTop: 2, marginBottom: 8 }}>
            Blocked or quarantined interceptor events, by risk category
          </div>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={owaspData} margin={{ top: 16, right: 8, left: -12, bottom: 0 }}>
              <CartesianGrid vertical={false} stroke={colors.borderFaint} />
              <XAxis dataKey="category" interval={0} tick={{ fontSize: 10.5, fill: colors.textFaint }} axisLine={{ stroke: colors.border }} tickLine={false} />
              <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: colors.textFaint }} axisLine={false} tickLine={false} width={28} />
              <Tooltip
                formatter={(value, _name, props) => [value, props.payload.label]}
                contentStyle={{ fontSize: 12, borderRadius: 8, border: `1px solid ${colors.border}` }}
              />
              <Bar dataKey="count" fill={colors.danger} radius={[4, 4, 0, 0]} maxBarSize={40}>
                <LabelList dataKey="count" position="top" style={{ fontSize: 11, fill: colors.textMuted }} />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
