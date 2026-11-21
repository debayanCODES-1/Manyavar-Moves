import { createHmac, timingSafeEqual } from "node:crypto";
import { handleUpload } from "@vercel/blob/client";

const SESSION_SECRET = process.env.APP_SESSION_SECRET || "";
const UPLOAD_ROLES = new Set(["admin", "stock_manager"]);

function header(request, name) {
  const headers = request.headers;
  if (typeof headers.get === "function") return headers.get(name) || "";
  return headers[name] || headers[name.toLowerCase()] || "";
}

function getUploadUser(request) {
  const cookie = header(request, "cookie")
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("stock_mover_session="))
    ?.slice("stock_mover_session=".length);
  const authorization = header(request, "authorization");
  const token = cookie || (authorization.startsWith("Bearer ") ? authorization.slice(7) : "");
  const [payload, signature] = token.split(".");
  if (!SESSION_SECRET || !payload || !signature) return null;

  const expected = createHmac("sha256", SESSION_SECRET).update(payload).digest("base64url");
  const expectedBytes = Buffer.from(expected);
  const suppliedBytes = Buffer.from(signature);
  if (suppliedBytes.length !== expectedBytes.length
      || !timingSafeEqual(suppliedBytes, expectedBytes)) return null;

  try {
    const session = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    if (session.expires <= Date.now() || !UPLOAD_ROLES.has(session.role)) return null;
    return session;
  } catch {
    return null;
  }
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    res.status(405).json({ error: "Method not allowed." });
    return;
  }
  if (!getUploadUser(req)) {
    res.status(401).json({ error: "Sign in with an upload-enabled role first." });
    return;
  }

  try {
    const body = typeof req.body === "string" ? JSON.parse(req.body) : req.body;
    const request = new Request(`https://${header(req, "host")}${req.url}`, {
      method: "POST",
      headers: req.headers,
      body: JSON.stringify(body),
    });
    const response = await handleUpload({
      body,
      request,
      onBeforeGenerateToken: async (pathname) => {
        if (!pathname.startsWith("datasets/")
            || !/\.(xlsx|zip)$/i.test(pathname)) {
          throw new Error("Only Excel workbooks and CSV ZIP archives can be uploaded.");
        }
        return {
          allowedContentTypes: [
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/zip",
            "application/x-zip-compressed",
            "application/octet-stream",
          ],
          maximumSizeInBytes: 200 * 1024 * 1024,
          addRandomSuffix: true,
        };
      },
      onUploadCompleted: async () => {},
    });
    res.status(200).json(response);
  } catch (error) {
    const message = error instanceof Error ? error.message : "The private upload could not be authorized.";
    console.error(`Private workbook upload failed: ${message}`);
    res.status(400).json({ error: message });
  }
}
