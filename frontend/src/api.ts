// Thin client for the backend API. Same-origin only; the session is an
// HttpOnly cookie, and every request carries the CSRF header the API requires.

const HEADERS = { "X-Requested-With": "rag-onprem" };

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, credentials: "same-origin", headers: { ...HEADERS } };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.headers = { ...HEADERS, "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(path, init);
  if (!resp.ok) {
    let message = resp.statusText;
    try {
      const data = await resp.json();
      if (typeof data.detail === "string") message = data.detail;
      else if (Array.isArray(data.detail)) message = data.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* not JSON */
    }
    throw new ApiError(resp.status, message);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

// ---------------------------------------------------------------- types

export interface GroupRef {
  id: number;
  name: string;
}

export interface Me {
  id: number;
  username: string;
  display_name: string | null;
  is_admin: boolean;
  groups: GroupRef[];
}

export interface Group extends GroupRef {
  members: number;
}

export interface DocumentInfo {
  id: number;
  filename: string;
  status: "pending" | "processing" | "ready" | "failed";
  error: string | null;
  content_type: string | null;
  size_bytes: number | null;
  page_count: number | null;
  chunks: number;
  stale: boolean;
  uploaded_by: number | null;
  group_ids: number[];
  can_manage: boolean;
  created_at: string;
  updated_at: string;
}

export interface Source {
  id: number;
  document: string;
  document_id: number;
  page: number | null;
  section: string | null;
  chunk_id: number;
  snippet: string;
  score: number;
}

export interface Answer {
  answer: string;
  found: boolean;
  sources: Source[];
}

export interface User {
  id: number;
  username: string;
  display_name: string | null;
  is_admin: boolean;
  disabled: boolean;
  group_ids: number[];
  created_at: string;
}

// ------------------------------------------------------------ endpoints

export const api = {
  me: () => request<Me>("GET", "/api/auth/me"),
  login: (username: string, password: string) =>
    request<Me>("POST", "/api/auth/login", { username, password }),
  logout: () => request<void>("POST", "/api/auth/logout"),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("POST", "/api/auth/password", { current_password, new_password }),
  groups: () => request<Group[]>("GET", "/api/groups"),

  documents: () => request<DocumentInfo[]>("GET", "/api/documents"),
  upload: (file: File, groupIds: number[]) => {
    const form = new FormData();
    form.append("file", file);
    groupIds.forEach((g) => form.append("group_ids", String(g)));
    return request<{ document: DocumentInfo; duplicate: boolean }>("POST", "/api/documents", form);
  },
  share: (id: number, group_ids: number[]) =>
    request<DocumentInfo>("PUT", `/api/documents/${id}/groups`, { group_ids }),
  reindex: (id: number) => request<DocumentInfo>("POST", `/api/documents/${id}/reindex`),
  deleteDocument: (id: number) => request<void>("DELETE", `/api/documents/${id}`),
  fileUrl: (id: number, page?: number | null) =>
    `/api/documents/${id}/file${page ? `#page=${page}` : ""}`,

  users: () => request<User[]>("GET", "/api/admin/users"),
  createUser: (body: {
    username: string;
    password: string;
    display_name?: string;
    is_admin: boolean;
    group_ids: number[];
  }) => request<User>("POST", "/api/admin/users", body),
  updateUser: (
    id: number,
    body: Partial<{ display_name: string; is_admin: boolean; disabled: boolean; group_ids: number[]; password: string }>,
  ) => request<User>("PATCH", `/api/admin/users/${id}`, body),
  deleteUser: (id: number) => request<void>("DELETE", `/api/admin/users/${id}`),
  adminGroups: () => request<Group[]>("GET", "/api/admin/groups"),
  createGroup: (name: string) => request<Group>("POST", "/api/admin/groups", { name }),
  deleteGroup: (id: number) => request<void>("DELETE", `/api/admin/groups/${id}`),
};

// ------------------------------------------------------------ streaming

export type AskEvent =
  | { type: "status"; stage: "retrieving" | "generating" }
  | { type: "token"; text: string }
  | ({ type: "done" } & Answer)
  | { type: "error"; message: string };

/** POST /api/ask/stream and parse the server-sent events. EventSource can't
 *  POST, so this reads the response body directly. */
export async function askStream(
  question: string,
  onEvent: (e: AskEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch("/api/ask/stream", {
    method: "POST",
    credentials: "same-origin",
    headers: { ...HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    let message = resp.statusText;
    try {
      message = (await resp.json()).detail ?? message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(resp.status, message);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let split: number;
    while ((split = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      let type = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent({ type, ...JSON.parse(data) } as AskEvent);
    }
  }
}
