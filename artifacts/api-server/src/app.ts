import express, { type Express } from "express";
import cors from "cors";
import pinoHttp from "pino-http";
import router from "./routes";
import { logger } from "./lib/logger";

const app: Express = express();
const miniAppOrigin =
  process.env["MINI_APP_PROXY_ORIGIN"] ?? "http://127.0.0.1:8000";

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return {
          id: req.id,
          method: req.method,
          url: req.url?.split("?")[0],
        };
      },
      res(res) {
        return {
          statusCode: res.statusCode,
        };
      },
    },
  }),
);
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use("/api", router);

/**
 * The Telegram Mini App is a Python service, while the workspace's public
 * Replit domain is currently owned by this artifact API service. Forward the
 * Mini App's root, static files, and API routes here so Telegram gets one
 * stable HTTPS origin. Keep /api/healthz local because the artifact workflow
 * uses it as its own health check.
 */
app.use(async (req, res, next) => {
  const isArtifactHealthCheck =
    req.path === "/api/healthz" && req.method === "GET";

  if (isArtifactHealthCheck) {
    next();
    return;
  }

  try {
    const headers: Record<string, string> = {};
    const contentType = req.get("content-type");
    const initData = req.get("x-telegram-init-data");

    if (contentType) headers["content-type"] = contentType;
    if (initData) headers["x-telegram-init-data"] = initData;

    let body: string | undefined;
    if (!["GET", "HEAD"].includes(req.method) && req.body !== undefined) {
      body = JSON.stringify(req.body);
      headers["content-type"] = "application/json";
    }

    const upstream = await fetch(`${miniAppOrigin}${req.originalUrl}`, {
      method: req.method,
      headers,
      body,
    });

    res.status(upstream.status);
    upstream.headers.forEach((value, key) => {
      if (key !== "content-length" && key !== "transfer-encoding") {
        res.setHeader(key, value);
      }
    });

    const responseBody = Buffer.from(await upstream.arrayBuffer());
    res.send(responseBody);
  } catch (error) {
    logger.error({ err: error, path: req.originalUrl }, "Mini App proxy failed");
    res.status(502).json({ detail: "Mini App backend is unavailable" });
  }
});

export default app;
