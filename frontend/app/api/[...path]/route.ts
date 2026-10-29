import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

const DROP = new Set(["host", "connection", "content-length", "transfer-encoding"]);

async function proxy(request: NextRequest, context: Context): Promise<Response> {
  const { path } = await context.params;
  const base = (process.env.API_INTERNAL_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
  const target = `${base}/api/${path.join("/")}${request.nextUrl.search}`;

  const headers = new Headers();
  request.headers.forEach((value, key) => {
    if (!DROP.has(key.toLowerCase())) headers.set(key, value);
  });

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const upstream = await fetch(target, {
    method: request.method,
    headers,
    body: hasBody ? await request.arrayBuffer() : undefined,
    cache: "no-store",
  });

  const out = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!DROP.has(key.toLowerCase())) out.set(key, value);
  });
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export const GET = proxy;
export const POST = proxy;
