import type { NextRequest } from "next/server";

/**
 * Per-IP rate limiter. Sliding window, in-memory.
 *
 * Each bucket (named) keeps its own state so endpoints can have different
 * budgets — e.g. /api/ask is expensive (Sonnet) and gets a smaller budget;
 * /api/explore/search is cheap (FTS) and gets a larger one.
 *
 * In-memory state is per-runtime-instance, so on Vercel a user may be able
 * to exceed the limit by hitting different functions. Good enough for a
 * small research tool; would graduate to Redis if traffic warranted it.
 */

interface BucketConfig {
  windowMs: number;
  max: number;
}

const buckets = new Map<string, Map<string, number[]>>();
const configs = new Map<string, BucketConfig>();

export function defineBucket(name: string, windowMs: number, max: number) {
  configs.set(name, { windowMs, max });
  if (!buckets.has(name)) buckets.set(name, new Map());
}

export function clientIp(req: NextRequest): string {
  return req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ?? "unknown";
}

export function checkRateLimit(bucketName: string, ip: string): boolean {
  const config = configs.get(bucketName);
  if (!config) throw new Error(`Unknown rate-limit bucket: ${bucketName}`);
  const ipMap = buckets.get(bucketName)!;

  const now = Date.now();
  const timestamps = ipMap.get(ip) ?? [];
  const recent = timestamps.filter((t) => now - t < config.windowMs);
  if (recent.length >= config.max) {
    ipMap.set(ip, recent);
    return false;
  }
  recent.push(now);
  ipMap.set(ip, recent);
  return true;
}

export function rateLimitResponse(): Response {
  return new Response(
    JSON.stringify({ error: "Rate limit exceeded. Try again in a minute." }),
    {
      status: 429,
      headers: { "Retry-After": "60", "Content-Type": "application/json" },
    }
  );
}

export function jsonError(message: string, status: number): Response {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// Define standard buckets. Each endpoint imports + uses these.
defineBucket("ask", 60_000, 10);           // expensive: Sonnet synthesis
defineBucket("cluster-story", 60_000, 15); // also Sonnet, but cached after first hit
defineBucket("brief", 60_000, 6);          // most expensive: Haiku + Sonnet + Voyage + heavy DB
defineBucket("search", 60_000, 60);        // cheap: just Postgres FTS
defineBucket("explore-read", 60_000, 120); // very cheap reads
