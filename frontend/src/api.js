import axios from 'axios';

const API_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';

export const api = axios.create({ baseURL: API_URL });

export const getHealth = () => api.get('/health');
export const listCases = () => api.get('/api/cases', { params: { limit: 200 } });
export const getCase = (traceId) => api.get(`/api/cases/${traceId}`);
export const getConsoleMetrics = () => api.get('/api/console-metrics');
export const queryAuditLog = (params) => api.get('/api/audit-log/query', { params });
export const queryGovernanceEvents = (params) => api.get('/api/governance-events', { params });
export const getPendingApprovals = () => api.get('/api/approvals/pending');
export const resolveApproval = (traceId, payload) => (
  api.post(`/api/approvals/${encodeURIComponent(traceId)}/resolve`, payload)
);

export default API_URL;
