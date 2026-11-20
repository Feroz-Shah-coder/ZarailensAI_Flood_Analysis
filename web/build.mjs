// Vercel build: copy the site to web/dist and write the Streamlit app URL into config.js.
// Set ZARAILENS_APP_URL in Vercel → Project → Settings → Environment Variables,
// e.g. https://zarailensai-app.onrender.com or https://zarailensai.streamlit.app
import { cpSync, mkdirSync, writeFileSync } from "node:fs";
const url = (process.env.ZARAILENS_APP_URL || "").trim().replace(/\/+$/, "");
if (url && !/^(https:\/\/|http:\/\/localhost)/.test(url)) throw new Error("ZARAILENS_APP_URL must start with https://");
if (!url) console.warn("ZARAILENS_APP_URL is not set; the site will show setup instructions.");
mkdirSync("web/dist", { recursive: true });
cpSync("web/public", "web/dist", { recursive: true });
writeFileSync("web/dist/config.js", `window.ZARAILENS_APP_URL = ${JSON.stringify(url)};\n`);
console.log("Built web/dist", url ? `→ ${url}` : "(no app URL)");
