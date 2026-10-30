const KEY_STORAGE = "apiKey";

function readKey(): string {
  try {
    return sessionStorage.getItem(KEY_STORAGE) ?? "";
  } catch {
    return "";
  }
}

export function setApiKey(key: string): void {
  try {
    sessionStorage.setItem(KEY_STORAGE, key);
  } catch {
    /* storage blocked: the key lasts until the next prompt */
  }
}

export function hasApiKey(): boolean {
  return readKey() !== "";
}

interface ApiOptions {
  method?: string;
  json?: unknown;
  body?: BodyInit;
}

function send(path: string, opts: ApiOptions): Promise<Response> {
  const headers: Record<string, string> = {};
  const key = readKey();
  if (key) headers["X-API-Key"] = key;
  let body = opts.body;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.json);
  }
  return fetch(path, { method: opts.method, headers, body });
}

export async function api<T>(path: string, opts: ApiOptions = {}): Promise<T> {
  let res = await send(path, opts);
  if (res.status === 401) {
    const key = window.prompt("This server requires an API key:");
    if (!key) throw new Error("API key required");
    setApiKey(key);
    res = await send(path, opts);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* non-JSON error body: keep the status text */
    }
    throw new Error(detail);
  }
  return (res.status === 204 ? null : await res.json()) as T;
}

export async function downloadFile(id: string, filename: string): Promise<void> {
  const res = await send(`/api/documents/${id}/file`, {});
  if (!res.ok) throw new Error(res.statusText);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
