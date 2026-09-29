export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function parseErrorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join("; ");
    }
  } catch {
    // fall through to generic message
  }
  return `Request failed with status ${res.status}`;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined>;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = new URL(`${API_BASE_URL}${path}`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/**
 * Thin fetch wrapper: JSON in, JSON out, consistent error shape. Callers still
 * own their own snake_case<->camelCase mapping in each domain's api.ts, same
 * as lib/icp/api.ts today — this wrapper only removes fetch/status/error
 * boilerplate, it is not a second mapping layer.
 */
export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const res = await fetch(buildUrl(path, options.query), {
    method: options.method ?? "GET",
    headers: options.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  if (!res.ok) {
    throw new ApiError(await parseErrorMessage(res), res.status);
  }

  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

/** For endpoints returning a non-JSON body (e.g. CSV export). */
export async function apiFetchRaw(path: string, options: RequestOptions = {}): Promise<Response> {
  const res = await fetch(buildUrl(path, options.query), { method: options.method ?? "GET" });
  if (!res.ok) {
    throw new ApiError(await parseErrorMessage(res), res.status);
  }
  return res;
}

export function buildApiUrl(path: string, query?: RequestOptions["query"]): string {
  return buildUrl(path, query);
}
