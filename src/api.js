// api.js — centralized API client with JWT auth injection

let _token = localStorage.getItem("insightflow_token") || null;

export function setToken(t) { _token = t; }
export function getToken() { return _token; }

function authHeader() {
  return _token ? { Authorization: `Bearer ${_token}` } : {};
}

async function apiFetch(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...authHeader(),
      ...(options.headers || {}),
    },
  });
  if (res && res.status === 401) {
    // Token expired — clear and reload to login
    _token = null;
    localStorage.removeItem("insightflow_token");
    window.location.reload();
    return null;
  }
  return res;
}

// ── Auth ─────────────────────────────────────────────────
export async function apiLogin(email, password) {
  const res = await apiFetch("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  return res.json();
}

export async function apiSignup(email, password) {
  const res = await apiFetch("/api/auth/signup", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  return res.json();
}

export async function apiMe() {
  const res = await apiFetch("/api/auth/me");
  if (!res) return null;
  return res.json();
}

// ── Settings ─────────────────────────────────────────────
export async function apiGetSettings() {
  const res = await apiFetch("/api/settings");
  if (!res) return null;
  return res.json();
}

export async function apiSaveSettings(settings) {
  const res = await apiFetch("/api/settings", {
    method: "PUT",
    body: JSON.stringify(settings),
  });
  if (!res) return null;
  return res.json();
}

// ── Plans ─────────────────────────────────────────────────
export async function apiGetPlans() {
  const res = await apiFetch("/api/plans");
  if (!res) return [];
  return res.json();
}

export async function apiSavePlan(plan, isNew = false) {
  const usePut = plan.id && !plan.id.startsWith("new_") && isNaN(plan.id) && !isNew;
  const res = await apiFetch(usePut ? `/api/plans/${plan.id}` : "/api/plans", {
    method: usePut ? "PUT" : "POST",
    body: JSON.stringify(plan),
  });
  if (!res) return null;
  return res.json();
}

export async function apiDeletePlan(planId) {
  const res = await apiFetch(`/api/plans/${planId}`, { method: "DELETE" });
  if (!res) return null;
  return res.json();
}

export async function apiRunPlan(planId) {
  const res = await apiFetch(`/api/plans/${planId}/run`, { method: "POST" });
  if (!res) return null;
  return res.json();
}

// ── Articles ─────────────────────────────────────────────
export async function apiGetArticles(planId) {
  const url = planId ? `/api/articles?planId=${encodeURIComponent(planId)}` : "/api/articles";
  const res = await apiFetch(url);
  if (!res) return [];
  return res.json();
}

export async function apiSaveArticle(article) {
  const res = await apiFetch("/api/articles", {
    method: "POST",
    body: JSON.stringify(article),
  });
  if (!res) return null;
  return res.json();
}

export async function apiDeleteArticles(planId) {
  const url = planId ? `/api/articles?planId=${encodeURIComponent(planId)}` : "/api/articles";
  const res = await apiFetch(url, { method: "DELETE" });
  if (!res) return null;
  return res.json();
}

export async function apiDeleteArticleById(id) {
  const res = await apiFetch(`/api/articles?id=${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!res) return null;
  return res.json();
}

// ── Email log ─────────────────────────────────────────────
export async function apiGetEmailLog() {
  const res = await apiFetch("/api/email-log");
  if (!res) return [];
  return res.json();
}

// ── Activity log ──────────────────────────────────────────
export async function apiGetLogs() {
  const res = await apiFetch("/api/logs");
  if (!res) return [];
  return res.json();
}

export async function apiAddLog(event, planName, type = "info") {
  await apiFetch("/api/logs", {
    method: "POST",
    body: JSON.stringify({ event, planName, type }),
  });
}

// ── AI call ───────────────────────────────────────────────
export async function apiCallAI(provider, systemPrompt, userPrompt, model) {
  const res = await apiFetch("/api/call-ai", {
    method: "POST",
    body: JSON.stringify({ provider, systemPrompt, userPrompt, model }),
  });
  if (!res) throw new Error("Network error");
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw Object.assign(new Error(err.error || `HTTP ${res.status}`), { errType: err.errType });
  }
  return res.json();
}

// ── Key validation ────────────────────────────────────────
export async function apiValidateKey(provider) {
  const res = await apiFetch("/api/validate-key", {
    method: "POST",
    body: JSON.stringify({ provider }),
  });
  if (!res) return { success: false };
  return res.json();
}

// ── Send email ────────────────────────────────────────────
export async function apiSendEmail({ planId, to, subject, html, articlesCount }) {
  const res = await apiFetch("/api/send-email", {
    method: "POST",
    body: JSON.stringify({ plan_id: planId, to, subject, html, articles_count: articlesCount }),
  });
  if (!res) throw new Error("Network error");
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

// ── URL proxy ─────────────────────────────────────────────
export async function apiFetchUrl(url) {
  const res = await apiFetch(`/api/fetch-url?url=${encodeURIComponent(url)}`, {
    signal: AbortSignal.timeout(15000),
  });
  if (!res || !res.ok) {
    const err = res ? await res.json().catch(() => ({})) : {};
    throw new Error(err.error || "Failed to fetch URL");
  }
  return res.text();
}

// ── Clear all data ────────────────────────────────────────
export async function apiClearData() {
  const res = await apiFetch("/api/data", { method: "DELETE" });
  if (!res) return null;
  return res.json();
}

export async function apiClearLogs() {
  const res = await apiFetch("/api/logs", { method: "DELETE" });
  if (!res) return null;
  return res.json();
}

export { apiFetch };
export function clearToken() { _token = null; }
