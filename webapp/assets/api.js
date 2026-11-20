// API client: JSON requests, analyst token, file downloads.
const TOKEN_KEY = "zl_token";
export const token = {
  get() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } },
  set(v) { try { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY); } catch {} },
};

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export async function api(path, { method = "GET", body, signal } = {}) {
  const headers = { "Accept": "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const t = token.get(); if (t) headers["x-admin-token"] = t;
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError("Network error – check your connection and try again.", 0);
  }
  const text = await res.text();
  let data = null; try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    let msg = (data && data.detail) || text || res.statusText;
    if (res.status === 504) msg = "The server took too long (Vercel time limit). Try a shorter period or try again.";
    throw new ApiError(typeof msg === "string" ? msg : JSON.stringify(msg), res.status);
  }
  return data;
}
export const post = (path, body, opts = {}) => api(path, { ...opts, method: "POST", body: body ?? {} });

/** POST a request whose response is a file, then save it in the browser. */
export async function download(path, body, filename) {
  const headers = { "Content-Type": "application/json" };
  const t = token.get(); if (t) headers["x-admin-token"] = t;
  const res = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  if (!res.ok) {
    let msg = res.statusText; try { msg = (await res.json()).detail || msg; } catch {}
    throw new ApiError(msg, res.status);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

let metaPromise = null;
export function meta() { return metaPromise ??= api("/api/meta"); }
let districtsPromise = null;
export function districtsGeo() { return districtsPromise ??= api("/api/geo/districts"); }
