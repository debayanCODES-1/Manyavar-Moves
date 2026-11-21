import { writeFile } from "node:fs/promises";

const isVercel = Boolean(process.env.VERCEL);
const configuredApiUrl = process.env.PUBLIC_API_BASE_URL?.trim();

if (isVercel && !configuredApiUrl) {
  throw new Error(
    "Set PUBLIC_API_BASE_URL in Vercel to the deployed Render gateway URL before building.",
  );
}

let apiBaseUrl = configuredApiUrl || "";
if (isVercel) {
  let parsedUrl;
  try {
    parsedUrl = new URL(apiBaseUrl);
  } catch {
    throw new Error("PUBLIC_API_BASE_URL must be an absolute HTTPS URL on Vercel.");
  }
  if (parsedUrl.protocol !== "https:" || parsedUrl.username || parsedUrl.password) {
    throw new Error("PUBLIC_API_BASE_URL must be an absolute HTTPS URL without credentials.");
  }
  apiBaseUrl = parsedUrl.origin;
}

await writeFile(
  new URL("../public/runtime-config.js", import.meta.url),
  `window.__MANYAVAR_CONFIG__=${JSON.stringify({ apiBaseUrl })};\n`,
  "utf8",
);
console.log(`Frontend configured for ${apiBaseUrl || "same-origin API"}.`);
