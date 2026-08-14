import axios from 'axios';

const API_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';

export const api = axios.create({ baseURL: API_URL });

export const getHealth = () => api.get('/health');
export const listCases = () => api.get('/api/cases', { params: { limit: 200 } });
export const getCase = (traceId) => api.get(`/api/cases/${traceId}`);
export const decideCase = (traceId, action, reviewer, notes, amount) =>
  api.post(`/api/cases/${traceId}/decision`, { action, reviewer, notes, amount });
export const getConsoleMetrics = () => api.get('/api/console-metrics');
export const queryAuditLog = (params) => api.get('/api/audit-log/query', { params });

export default API_URL;
