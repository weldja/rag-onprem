import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type DocSource, type DocumentInfo, type Me } from "./api";

/** What the next questions are limited to: a folder, a share or one document. */
export interface Scope {
  key: string;
  label: string;
  ids: number[];
}

interface Folder {
  name: string;
  path: string;
  folders: Folder[];
  files: DocumentInfo[];
  total: number;
}

const BUSY = new Set(["pending", "processing"]);
// Folders with this many documents or fewer start expanded (as in rag-claude).
const OPEN_UP_TO = 15;

function buildTree(docs: DocumentInfo[]): Folder {
  const root: Folder = { name: "", path: "", folders: [], files: [], total: 0 };
  for (const d of docs) {
    const parts = (d.source_path ?? d.filename).split("/");
    let node = root;
    node.total++;
    for (let i = 0; i < parts.length - 1; i++) {
      const path = parts.slice(0, i + 1).join("/");
      let child = node.folders.find((f) => f.path === path);
      if (!child) {
        child = { name: parts[i], path, folders: [], files: [], total: 0 };
        node.folders.push(child);
      }
      child.total++;
      node = child;
    }
    node.files.push(d);
  }
  const sort = (f: Folder) => {
    f.folders.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
    f.files.sort((a, b) => a.filename.localeCompare(b.filename, undefined, { sensitivity: "base" }));
    f.folders.forEach(sort);
  };
  sort(root);
  return root;
}

function readyIds(docs: DocumentInfo[]): number[] {
  return docs.filter((d) => d.status === "ready").map((d) => d.id);
}

function folderDocs(f: Folder): DocumentInfo[] {
  return [...f.files, ...f.folders.flatMap(folderDocs)];
}

function ago(iso: string | null): string {
  if (!iso) return "not yet";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

function Counts({ docs }: { docs: DocumentInfo[] }) {
  const ready = docs.filter((d) => d.status === "ready").length;
  const busy = docs.filter((d) => BUSY.has(d.status)).length;
  const failed = docs.filter((d) => d.status === "failed").length;
  return (
    <p className="src-counts">
      <span className="dot ready" aria-hidden /> {ready} ready
      {busy > 0 && (
        <>
          {" · "}
          <span className="dot busy" aria-hidden /> {busy} indexing
        </>
      )}
      {failed > 0 && (
        <>
          {" · "}
          <span className="dot failed" aria-hidden /> {failed} failed
        </>
      )}
    </p>
  );
}

function FileRow({
  doc,
  scope,
  onAsk,
  onSummarise,
}: {
  doc: DocumentInfo;
  scope: Scope | null;
  onAsk: (s: Scope) => void;
  onSummarise: (d: DocumentInfo) => void;
}) {
  const ready = doc.status === "ready";
  const key = `doc:${doc.id}`;
  const status = doc.error ? `${doc.status}: ${doc.error}` : doc.status;
  return (
    <li className={scope?.key === key ? "file selected" : "file"}>
      <span className={`dot ${BUSY.has(doc.status) ? "busy" : doc.status}`} title={status} aria-label={status} />
      <button
        className="file-name"
        disabled={!ready}
        title={ready ? `Ask about ${doc.filename}` : status}
        onClick={() => onAsk({ key, label: doc.filename, ids: [doc.id] })}
      >
        {doc.filename}
      </button>
      <span className="row-actions">
        {ready && (
          <button className="link" title={`Summarise ${doc.filename}`} onClick={() => onSummarise(doc)}>
            Summary
          </button>
        )}
        <a href={api.fileUrl(doc.id)} target="_blank" rel="noreferrer" title="Open the file">
          Open
        </a>
      </span>
    </li>
  );
}

function FolderNode({
  folder,
  srcKey,
  open,
  toggle,
  forceOpen,
  ...rest
}: {
  folder: Folder;
  srcKey: string;
  open: (key: string, total: number) => boolean;
  toggle: (key: string, total: number) => void;
  forceOpen: boolean;
  scope: Scope | null;
  onAsk: (s: Scope) => void;
  onSummarise: (d: DocumentInfo) => void;
}) {
  const key = `${srcKey}:${folder.path}`;
  const isOpen = forceOpen || open(key, folder.total);
  const ids = readyIds(folderDocs(folder));
  const scopeKey = `folder:${key}`;
  return (
    <li className="folder">
      <div className={rest.scope?.key === scopeKey ? "folder-row selected" : "folder-row"}>
        <button className="twisty" aria-expanded={isOpen} onClick={() => toggle(key, folder.total)}>
          <span className="chevron" aria-hidden>
            {isOpen ? "▾" : "▸"}
          </span>
          <span className="folder-name">{folder.name}</span>
          <span className="count">{folder.total}</span>
        </button>
        <span className="row-actions">
          <button
            className="link"
            disabled={ids.length === 0}
            title={`Ask about the documents in ${folder.path}`}
            onClick={() => rest.onAsk({ key: scopeKey, label: folder.path, ids })}
          >
            Ask
          </button>
        </span>
      </div>
      {isOpen && <TreeList folder={folder} srcKey={srcKey} open={open} toggle={toggle} forceOpen={forceOpen} {...rest} />}
    </li>
  );
}

function TreeList(props: Parameters<typeof FolderNode>[0]) {
  const { folder, ...rest } = props;
  return (
    <ul className="tree">
      {folder.folders.map((f) => (
        <FolderNode key={f.path} folder={f} {...rest} />
      ))}
      {folder.files.map((d) => (
        <FileRow key={d.id} doc={d} scope={rest.scope} onAsk={rest.onAsk} onSummarise={rest.onSummarise} />
      ))}
    </ul>
  );
}

export default function Sidebar({
  me,
  scope,
  onAsk,
  onSummarise,
}: {
  me: Me;
  scope: Scope | null;
  onAsk: (s: Scope) => void;
  onSummarise: (d: DocumentInfo) => void;
}) {
  const [docs, setDocs] = useState<DocumentInfo[]>([]);
  const [sources, setSources] = useState<DocSource[]>([]);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");
  const [overrides, setOverrides] = useState<Map<string, boolean>>(new Map());
  const loaded = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const [d, s] = await Promise.all([api.documents(), api.sources()]);
      setDocs(d);
      setSources(s);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't load documents");
    } finally {
      loaded.current = true;
    }
  }, []);

  // Poll quickly while something is indexing or a scan was asked for, slowly
  // otherwise, and not at all while the tab is in the background.
  const busy = docs.some((d) => BUSY.has(d.status)) || sources.some((s) => s.scan_requested);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  useEffect(() => {
    const timer = setInterval(
      () => {
        if (!document.hidden) void refresh();
      },
      busy ? 4000 : 30000,
    );
    return () => clearInterval(timer);
  }, [busy, refresh]);

  const needle = filter.trim().toLowerCase();
  const shown = useMemo(
    () =>
      needle
        ? docs.filter((d) => (d.source_path ?? d.filename).toLowerCase().includes(needle))
        : docs,
    [docs, needle],
  );

  const open = (key: string, total: number) => overrides.get(key) ?? total <= OPEN_UP_TO;
  const toggle = (key: string, total: number) =>
    setOverrides((m) => new Map(m).set(key, !open(key, total)));

  const act = async (fn: () => Promise<unknown>, confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    }
    void refresh();
  };

  const known = new Set(sources.map((s) => s.id));
  const uploads = shown.filter((d) => d.source_id === null || !known.has(d.source_id));
  const treeProps = { open, toggle, forceOpen: needle !== "", scope, onAsk, onSummarise };

  return (
    <aside className="sidebar" aria-label="Document sources">
      <div className="sidebar-head">
        <h2>Documents</h2>
        <input
          type="search"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter by name or folder"
          aria-label="Filter documents"
        />
      </div>
      {error && (
        <p className="error small" role="alert">
          {error}
        </p>
      )}

      {sources.map((s) => {
        const all = docs.filter((d) => d.source_id === s.id);
        const tree = buildTree(shown.filter((d) => d.source_id === s.id));
        const scopeKey = `source:${s.id}`;
        return (
          <section key={s.id} className={s.active ? "src" : "src inactive"}>
            <div className={scope?.key === scopeKey ? "src-head selected" : "src-head"}>
              <h3 title={s.name}>{s.name}</h3>
              <button
                className="link"
                disabled={readyIds(all).length === 0}
                title={`Ask about everything in ${s.name}`}
                onClick={() => onAsk({ key: scopeKey, label: s.name, ids: readyIds(all) })}
              >
                Ask
              </button>
            </div>
            <code className="src-path" title="Where these documents are indexed from">
              {s.display_path}
            </code>
            <Counts docs={all} />
            <p className="meta small">
              {s.active ? (
                <>
                  Checked {ago(s.last_scan_at)}
                  {s.files_skipped > 0 && ` · ${s.files_skipped} unsupported or too large`}
                </>
              ) : (
                "No longer configured"
              )}
              {me.is_admin && s.active && (
                <button className="link" disabled={s.scan_requested} onClick={() => act(() => api.scanSource(s.id))}>
                  {s.scan_requested ? "Scanning…" : "Scan now"}
                </button>
              )}
              {me.is_admin && !s.active && (
                <button
                  className="link danger"
                  onClick={() =>
                    act(
                      () => api.removeSource(s.id),
                      `Remove ${s.name} and its ${all.length} documents from the index? The files themselves are not touched.`,
                    )
                  }
                >
                  Remove
                </button>
              )}
            </p>
            {s.last_scan_error && <p className="src-error">{s.last_scan_error}</p>}
            {tree.total === 0 ? (
              <p className="meta small">{needle ? "No matches" : s.last_scan_at ? "No documents found" : "Waiting for the first scan…"}</p>
            ) : (
              <TreeList folder={tree} srcKey={s.key} {...treeProps} />
            )}
          </section>
        );
      })}

      {(uploads.length > 0 || sources.length === 0) && (
        <section className="src">
          <div className="src-head">
            <h3>Uploaded</h3>
          </div>
          {uploads.length > 0 && <Counts docs={uploads} />}
          {uploads.length === 0 ? (
            <p className="meta small">
              {!loaded.current ? "Loading…" : needle ? "No matches" : "No documents yet. Add some on the Documents tab."}
            </p>
          ) : (
            <TreeList folder={buildTree(uploads.map((d) => ({ ...d, source_path: null })))} srcKey="uploads" {...treeProps} />
          )}
        </section>
      )}
    </aside>
  );
}
