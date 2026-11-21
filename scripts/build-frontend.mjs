import { writeFile } from "node:fs/promises";
import { build } from "esbuild";

const isVercel = Boolean(process.env.VERCEL);
const configuredApiUrl = process.env.PUBLIC_API_BASE_URL?.trim();

let apiBaseUrl = configuredApiUrl && configuredApiUrl !== "/" ? configuredApiUrl : "";
if (apiBaseUrl) {
  let parsedUrl;
  try {
    parsedUrl = new URL(apiBaseUrl);
  } catch {
    throw new Error("PUBLIC_API_BASE_URL must be an absolute HTTPS URL.");
  }
  if (parsedUrl.protocol !== "https:" || parsedUrl.username || parsedUrl.password) {
    throw new Error("PUBLIC_API_BASE_URL must be an absolute HTTPS URL without credentials.");
  }
  apiBaseUrl = parsedUrl.origin;
}

await build({
  entryPoints: ["public/app.js"],
  bundle: true,
  format: "esm",
  platform: "browser",
  target: "es2022",
  outfile: "public/app.bundle.js",
});

await writeFile(
  new URL("../public/runtime-config.js", import.meta.url),
  `window.__MANYAVAR_CONFIG__=${JSON.stringify({
    apiBaseUrl,
    blobUploads: isVercel,
  })};\n`,
  "utf8",
);
console.log(`Frontend configured for ${apiBaseUrl || "same-origin API"}${isVercel ? " with private Blob uploads" : ""}.`);
