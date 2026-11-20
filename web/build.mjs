// Vercel build: copy web/public to web/dist and write the Streamlit app URL into config.js.
// Works whether Vercel's Root Directory is the repo root or "web".
// Set ZARAILENS_APP_URL in Vercel → Project → Settings → Environment Variables,
// e.g. https://zarailensai-app.onrender.com or https://zarailensai.streamlit.app
import { cpSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, "dist");
const url = (process.env.ZARAILENS_APP_URL || "").trim().replace(/\/+$/, "");
if (url && !/^(https:\/\/|http:\/\/localhost)/.test(url)) throw new Error("ZARAILENS_APP_URL must start with https://");
if (!url) console.warn("ZARAILENS_APP_URL is not set; the site will show setup instructions.");
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
cpSync(join(here, "public"), out, { recursive: true });
writeFileSync(join(out, "config.js"), `window.ZARAILENS_APP_URL = ${JSON.stringify(url)};\n`);
console.log("Built", out, url ? `→ ${url}` : "(no app URL)");
