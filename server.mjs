import { createReadStream, readFileSync, statSync } from "node:fs";
import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { createServer, request as proxyRequest } from "node:http";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL(".", import.meta.url));
const PUBLIC_DIR = resolve(ROOT, "public");
loadLocalEnv();
const BACKEND = new URL(process.env.FASTAPI_URL || "http://127.0.0.1:8000");
const PORT = Number(process.env.PORT || 3000);
const MAX_UPLOAD = 210 * 1024 * 1024;
const DEMO_MODE = process.env.DEMO_MODE !== "false";
const APPROVAL_TOKEN = process.env.APPROVAL_TOKEN || "";
const SESSION_SECRET = process.env.APP_SESSION_SECRET || APPROVAL_TOKEN || randomBytes(32).toString("hex");
const DEMO_ROLES = {
  admin: { name: "Demo Admin", permissions: ["read", "upload", "run", "approve", "ask"] },
  stock_manager: { name: "Stock Manager", permissions: ["read", "upload", "run", "approve", "ask"] },
  validator: { name: "Rule Validator", permissions: ["read", "approve", "ask"] },
  area_manager: { name: "Area Manager", permissions: ["read", "approve", "ask"] },
  analyst: { name: "Sales Analyst", permissions: ["read", "ask"] },
  viewer: { name: "Read-only Viewer", permissions: ["read"] },
};
const MIME = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml",
  ".bin": "application/octet-stream",
};

function proxy(req, res) {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  if (url.pathname === "/api/auth/status" && req.method === "GET") {
    const user = getSession(req);
    sendJson(res, 200, {
      required: DEMO_MODE || Boolean(APPROVAL_TOKEN),
      demoMode: DEMO_MODE,
      authenticated: !DEMO_MODE && !APPROVAL_TOKEN || Boolean(user),
      user,
      roles: Object.entries(DEMO_ROLES).map(([id, value]) => ({ id, name: value.name })),
    });
    return;
  }
  if (url.pathname === "/api/auth/demo-login" && req.method === "POST") {
    demoLogin(req, res);
    return;
  }
  if (url.pathname === "/api/auth/login" && req.method === "POST") {
    tokenLogin(req, res);
    return;
  }
  if (url.pathname === "/api/auth/logout" && req.method === "POST") {
    res.writeHead(204, { "set-cookie": "stock_mover_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0" });
    res.end();
    return;
  }
  if (url.pathname.startsWith("/api/") && url.pathname !== "/api/health"
      && !url.pathname.startsWith("/api/auth/")
      && (DEMO_MODE || APPROVAL_TOKEN)) {
    const user = getSession(req);
    if (!user) {
      sendJson(res, 401, { detail: "Sign in with a demo role to access merchandising data." });
      return;
    }
    const requiredPermission = requestPermission(req.method, url.pathname);
    if (requiredPermission && !DEMO_ROLES[user.role]?.permissions.includes(requiredPermission)) {
      sendJson(res, 403, { detail: `Your ${DEMO_ROLES[user.role]?.name || "user"} role cannot ${requiredPermission} this resource.` });
      return;
    }
  }
  const user = getSession(req);
  const backendPath = url.pathname === "/api/health"
    ? "/health"
    : `${url.pathname}${url.search}`;
  const length = Number(req.headers["content-length"] || 0);
  if (length > MAX_UPLOAD) {
    res.writeHead(413, { "content-type": "application/json" });
    res.end(JSON.stringify({ detail: "Upload exceeds the 200 MB limit." }));
    return;
  }
  const outgoing = proxyRequest({
    hostname: BACKEND.hostname,
    port: BACKEND.port || 80,
    path: backendPath,
    method: req.method,
    headers: {
      "content-type": req.headers["content-type"] || "application/octet-stream",
      ...(req.headers["content-length"] ? { "content-length": req.headers["content-length"] } : {}),
      accept: req.headers.accept || "application/json",
      ...(user ? {
        "x-stockmover-user": user.name,
        "x-stockmover-role": user.role,
        ...(user.area_manager_id ? { "x-stockmover-area-manager": user.area_manager_id } : {}),
      } : {}),
    },
  }, (upstream) => {
    res.writeHead(upstream.statusCode || 502, {
      "content-type": upstream.headers["content-type"] || "application/json",
      "content-disposition": upstream.headers["content-disposition"] || "",
      "cache-control": "no-store",
    });
    upstream.pipe(res);
  });
  outgoing.on("error", () => {
    if (!res.headersSent) {
      res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ detail: "FastAPI is unavailable. Start the API service and retry." }));
    } else {
      res.destroy();
    }
  });
  req.pipe(outgoing);
}

function sendJson(res, status, value, headers = {}) {
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store", ...headers });
  res.end(JSON.stringify(value));
}

function cookies(req) {
  return Object.fromEntries((req.headers.cookie || "").split(";").map((part) => {
    const index = part.indexOf("=");
    return index < 0 ? ["", ""] : [part.slice(0, index).trim(), part.slice(index + 1).trim()];
  }));
}

function getSession(req) {
  const value = cookies(req).stock_mover_session || "";
  const [payload, signature] = value.split(".");
  if (!payload || !signature) return null;
  const expected = createHmac("sha256", SESSION_SECRET).update(payload).digest("base64url");
  const receivedBytes = Buffer.from(signature);
  const expectedBytes = Buffer.from(expected);
  if (receivedBytes.length !== expectedBytes.length || !timingSafeEqual(receivedBytes, expectedBytes)) return null;
  try {
    const session = JSON.parse(Buffer.from(payload, "base64url").toString());
    if (session.expires <= Date.now() || !DEMO_ROLES[session.role]) return null;
    return {
      name: session.name, role: session.role,
      area_manager_id: session.area_manager_id || null,
      permissions: DEMO_ROLES[session.role].permissions,
    };
  } catch {
    return null;
  }
}

function requestPermission(method, pathname) {
  if (pathname === "/api/upload" && method === "POST") return "upload";
  if (pathname === "/api/run" && method === "POST") return "run";
  if (pathname === "/api/ask" && method === "POST") return "ask";
  if (pathname.startsWith("/api/recommendations/") && method === "PATCH") return "approve";
  if (pathname.startsWith("/api/logistics/") && method === "POST") return "approve";
  return null;
}

function readBody(req, callback) {
  let body = "";
  req.on("data", (chunk) => {
    body += chunk;
    if (body.length > 2048) req.destroy();
  });
  req.on("end", () => {
    try {
      callback(JSON.parse(body));
    } catch {
      callback(null);
    }
  });
}

function issueSession(req, res, role, name, areaManagerId = null) {
  const payload = Buffer.from(JSON.stringify({
    name, role, area_manager_id: areaManagerId, expires: Date.now() + 8 * 60 * 60 * 1000,
  })).toString("base64url");
  const signature = createHmac("sha256", SESSION_SECRET).update(payload).digest("base64url");
  const secure = req.headers["x-forwarded-proto"] === "https" ? "; Secure" : "";
  sendJson(res, 200, {
    ok: true,
    user: { name, role, area_manager_id: areaManagerId, permissions: DEMO_ROLES[role].permissions },
  }, {
    "set-cookie": `stock_mover_session=${payload}.${signature}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800${secure}`,
  });
}

function demoLogin(req, res) {
  if (!DEMO_MODE) {
    sendJson(res, 404, { detail: "Demo sign-in is disabled." });
    return;
  }
  readBody(req, (body) => {
    if (!body || !DEMO_ROLES[body.role]) {
      sendJson(res, 400, { detail: "Choose a valid demo role." });
      return;
    }
    if (APPROVAL_TOKEN) {
      const incoming = Buffer.from(String(body.token || ""));
      const expected = Buffer.from(APPROVAL_TOKEN);
      if (incoming.length !== expected.length || !timingSafeEqual(incoming, expected)) {
        sendJson(res, 401, { detail: "A valid APPROVAL_TOKEN is required to start a demo session." });
        return;
      }
    }
    if (body.role === "area_manager" && !/^AM-\d{2}$/.test(String(body.area_manager_id || ""))) {
      sendJson(res, 400, { detail: "Select an area manager account." });
      return;
    }
    const areaManagerId = body.role === "area_manager" ? body.area_manager_id : null;
    const name = areaManagerId ? `${DEMO_ROLES[body.role].name} · ${areaManagerId}` : DEMO_ROLES[body.role].name;
    issueSession(req, res, body.role, name, areaManagerId);
  });
}

function tokenLogin(req, res) {
  readBody(req, (body) => {
    if (!APPROVAL_TOKEN) {
      sendJson(res, 404, { detail: "Token sign-in is not configured." });
      return;
    }
    const incoming = Buffer.from(String(body?.token || ""));
    const expected = Buffer.from(APPROVAL_TOKEN);
    if (incoming.length !== expected.length || !timingSafeEqual(incoming, expected)) {
      sendJson(res, 401, { detail: "That access token was not recognised." });
      return;
    }
    const role = DEMO_MODE && DEMO_ROLES[body?.role] ? body.role : "admin";
    issueSession(req, res, role, DEMO_MODE ? DEMO_ROLES[role].name : "Administrator",
      role === "area_manager" ? body?.area_manager_id : null);
  });
}

createServer((req, res) => {
  if (req.url === "/api" || req.url.startsWith("/api/")) {
    proxy(req, res);
    return;
  }
  if (req.url === "/health") {
    req.url = "/api/health";
    proxy(req, res);
    return;
  }
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { allow: "GET, HEAD" });
    res.end();
    return;
  }
  let pathname;
  try {
    pathname = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
  } catch {
    res.writeHead(400, { "content-type": "text/plain; charset=utf-8" });
    res.end("Invalid path");
    return;
  }
  const requested = pathname === "/" ? "/index.html" : pathname;
  const file = resolve(PUBLIC_DIR, `.${requested}`);
  if (!file.startsWith(`${PUBLIC_DIR}${sep}`) || !statExists(file)) {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    res.end("Not found");
    return;
  }
  res.writeHead(200, {
    "content-type": MIME[extname(file)] || "application/octet-stream",
    "cache-control": extname(file) === ".html" ? "no-cache" : "public, max-age=3600",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
  });
  if (req.method === "HEAD") res.end();
  else createReadStream(file).pipe(res);
}).listen(PORT, "0.0.0.0", () => {
  console.log(`Manyavar Stock Mover listening at http://localhost:${PORT}`);
});

function statExists(file) {
  try {
    return statSync(file).isFile();
  } catch {
    return false;
  }
}

function loadLocalEnv() {
  try {
    const contents = readFileSync(resolve(ROOT, ".env"), "utf8");
    for (const line of contents.split(/\r?\n/)) {
      const match = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
      if (!match || process.env[match[1]] !== undefined) continue;
      const value = match[2].replace(/^(['"])(.*)\1$/, "$2");
      process.env[match[1]] = value;
    }
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
}
