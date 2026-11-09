import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type DocSource,
  type DocumentInfo,
  type IndexAction,
  type SkippedFile,
} from "../api";

// One row per file: an indexable document (with actions) or a share file that
// isn't indexed (read-only, with the reason).
type Row =
  | { kind: "doc"; key: string; doc: DocumentInfo; path: string; source: number | null }
  | { kind: "skip"; key: string; file: SkippedFile; path: string; source: number };

type StatusFilter = "all" | "processing" | "pending" | "failed" | "ready" | "excluded" | "skipped";
type SortKey = "status" | "path" | "excerpts" | "size" | "updated";

const STATUS_LABEL: Record<Exclude<StatusFilter, "all">, string> = {
  processing: "Indexing",
  pending: "Queued",
  failed: "Failed",
  ready: "Indexed",
  excluded: "Excluded",
  skipped: "Not indexed",
};
const STATUS_ORDER: Exclude<StatusFilter, "all">[] = [
  "processing",
  "pending",
  "failed",
  "ready",
  "excluded",
  "skipped",
];
const SKIP_REASON: Record<SkippedFile["reason"], string> = {
  unsupported: "file type not supported",
  empty: "empty file",
  too_large: "over the size limit (MAX_UPLOAD_MB)",
  excluded: "excluded by the share's SHARE_n_EXCLUDE setting",
};
const PAGE = 200;

function rowStatus(r: Row): Exclude<StatusFilter, "all"> {
  return r.kind === "skip" ? "skipped" : r.doc.status;
}

function size(bytes: number | null): string {
  if (bytes === null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function when(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  return today
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

/** Which actions make sense for a document in this state. */
function actionsFor(d: DocumentInfo): IndexAction[] {
  if (!d.can_manage) return [];
  switch (d.status) {
    case "ready":
    case "failed":
      return ["reindex", "exclude"];
    case "pending":
    case "processing":
      return ["exclude"];
    case "excluded":
      return ["include"];
  }
}

function actionLabel(a: IndexAction, d?: DocumentInfo): string {
  if (a === "reindex") return "Re-index";
  if (a === "include") return "Include";
  if (d?.status === "processing") return "Stop & exclude";
  if (d?.status === "pending") return "Remove from queue";
  return "Exclude";
}

function StatusCell({ row }: { row: Row }) {
  if (row.kind === "skip") {
    return (
      <>
        <span className="badge skipped">Not indexed</span>
        <div className="meta small">{SKIP_REASON[row.file.reason]}</div>
      </>
    );
  }
  const d = row.doc;
  const label = STATUS_LABEL[d.status];
  if (d.status === "processing") {
    const total = d.progress_total ?? 0;
    const done = d.progress_done ?? 0;
    const pct = total ? Math.round((done / total) * 100) : 0;
    return (
      <>
        <span className="badge processing">{label}</span>
        {total > 0 ? (
          <div className="progress" title={`${done} of ${total} excerpts`}>
            <progress max={total} value={done} aria-label={`${pct}% indexed`} />
            <span className="meta small">
              {done.toLocaleString()} / {total.toLocaleString()} excerpts ({pct}%)
            </span>
          </div>
        ) : (
          <div className="meta small">reading the file…</div>
        )}
      </>
    );
  }
  return (
    <>
      <span className={`badge ${d.status}`}>{label}</span>
      {d.status === "pending" && d.queue_position !== null && (
        <span className="meta small"> #{d.queue_position} in queue</span>
      )}
      {d.stale && <span className="badge failed">needs re-index</span>}
      {d.error && <div className="error small">{d.error}</div>}
    </>
  );
}

export default function Indexing() {
  const [docs, setDocs] = useState<DocumentInfo[]>([]);
  const [sources, setSources] = useState<DocSource[]>([]);
  const [skipped, setSkipped] = useState<SkippedFile[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [working, setWorking] = useState(false);

  const [q, setQ] = useState("");
  const [source, setSource] = useState<string>("all"); // all | uploads | <source id>
  const [status, setStatus] = useState<StatusFilter>("all");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "status", desc: false });
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [limit, setLimit] = useState(PAGE);

  const refreshDocs = useCallback(
    () =>
      api
        .documents()
        .then((d) => {
          setDocs(d);
          setLoaded(true);
        })
        .catch((e) => setError(e.message)),
    [],
  );
  const refreshShares = useCallback(
    () =>
      Promise.all([api.sources(), api.skippedFiles()])
        .then(([s, k]) => {
          setSources(s);
          setSkipped(k);
        })
        .catch((e) => setError(e.message)),
    [],
  );

  useEffect(() => {
    void refreshDocs();
    void refreshShares();
  }, [refreshDocs, refreshShares]);

  // Fast refresh while anything is queued or indexing; slow otherwise.
  const busy = docs.some((d) => d.status === "pending" || d.status === "processing");
  useEffect(() => {
    const t = setInterval(() => void refreshDocs(), busy ? 3000 : 15000);
    return () => clearInterval(t);
  }, [busy, refreshDocs]);
  useEffect(() => {
    const t = setInterval(() => void refreshShares(), 30000);
    return () => clearInterval(t);
  }, [refreshShares]);

  const sourceName = useMemo(() => new Map(sources.map((s) => [s.id, s.name])), [sources]);

  const rows: Row[] = useMemo(
    () => [
      ...docs.map(
        (doc): Row => ({
          kind: "doc",
          key: `d${doc.id}`,
          doc,
          path: doc.source_path ?? doc.filename,
          source: doc.source_id,
        }),
      ),
      ...skipped.map(
        (file): Row => ({
          kind: "skip",
          key: `s${file.source_id}:${file.path}`,
          file,
          path: file.path,
          source: file.source_id,
        }),
      ),
    ],
    [docs, skipped],
  );

  // Source and search filters first; the status counts reflect them.
  const scoped = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return rows.filter((r) => {
      if (source === "uploads" && r.source !== null) return false;
      if (source !== "all" && source !== "uploads" && r.source !== Number(source)) return false;
      return !needle || r.path.toLowerCase().includes(needle);
    });
  }, [rows, q, source]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const r of scoped) c[rowStatus(r)] = (c[rowStatus(r)] ?? 0) + 1;
    return c;
  }, [scoped]);

  const visible = useMemo(() => {
    const list = status === "all" ? scoped : scoped.filter((r) => rowStatus(r) === status);
    const key = (r: Row): number | string => {
      switch (sort.key) {
        case "path":
          return r.path.toLowerCase();
        case "excerpts":
          return r.kind === "doc" ? r.doc.chunks : -1;
        case "size":
          return (r.kind === "doc" ? r.doc.size_bytes : r.file.size_bytes) ?? -1;
        case "updated":
          return r.kind === "doc" ? (r.doc.indexed_at ?? r.doc.updated_at) : "";
        case "status": {
          const rank = STATUS_ORDER.indexOf(rowStatus(r)) * 1e7;
          const pos = r.kind === "doc" ? (r.doc.queue_position ?? 0) : 0;
          return rank + pos;
        }
      }
    };
    return [...list].sort((a, b) => {
      const ka = key(a);
      const kb = key(b);
      const c = ka < kb ? -1 : ka > kb ? 1 : a.path.localeCompare(b.path);
      return sort.desc ? -c : c;
    });
  }, [scoped, status, sort]);

  // Selection only ever holds documents the user can manage that are still listed.
  const docById = useMemo(() => new Map(docs.map((d) => [d.id, d])), [docs]);
  const selectedDocs = [...selected].map((id) => docById.get(id)).filter((d): d is DocumentInfo => !!d);
  const selectable = visible.flatMap((r) => (r.kind === "doc" && r.doc.can_manage ? [r.doc.id] : []));
  const allSelected = selectable.length > 0 && selectable.every((id) => selected.has(id));

  const toggle = (id: number, on: boolean) => {
    const next = new Set(selected);
    if (on) next.add(id);
    else next.delete(id);
    setSelected(next);
  };

  const run = async (action: IndexAction, targets: DocumentInfo[]) => {
    const eligible = targets.filter((d) => actionsFor(d).includes(action));
    if (eligible.length === 0) return;
    if (action === "exclude") {
      const running = eligible.filter((d) => d.status === "processing").length;
      const text =
        eligible.length === 1
          ? `Exclude ${eligible[0].filename}? It is removed from the index and won't be indexed again until you include it.`
          : `Exclude ${eligible.length} files? They are removed from the index and won't be indexed again until you include them.`;
      if (!window.confirm(text + (running ? ` Indexing in progress is stopped.` : ""))) return;
    }
    setWorking(true);
    setError("");
    setMessage("");
    try {
      const res = await api.indexAction(
        action,
        eligible.map((d) => d.id),
      );
      const verb = { reindex: "queued for re-indexing", exclude: "excluded", include: "queued for indexing" }[
        action
      ];
      let note = `${res.changed.length} file${res.changed.length === 1 ? "" : "s"} ${verb}.`;
      if (res.skipped.length) note += ` ${res.skipped.length} left unchanged (${res.skipped[0].reason}).`;
      setMessage(note);
      setSelected(new Set());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    }
    setWorking(false);
    void refreshDocs();
  };

  const sortBy = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, desc: !s.desc } : { key, desc: key === "updated" || key === "size" }));
  const sortMark = (key: SortKey) => (sort.key === key ? (sort.desc ? " ▾" : " ▴") : "");
  const header = (key: SortKey, label: string) => (
    <th aria-sort={sort.key === key ? (sort.desc ? "descending" : "ascending") : undefined}>
      <button className="link th-sort" onClick={() => sortBy(key)}>
        {label}
        {sortMark(key)}
      </button>
    </th>
  );

  const bulk: IndexAction[] = ["reindex", "exclude", "include"];
  const totalExcerpts = docs.reduce((n, d) => n + (d.status === "ready" ? d.chunks : 0), 0);

  return (
    <section className="page wide indexing">
      <h2>Indexing</h2>
      <p className="meta">
        Every file the assistant knows about: uploads and files on the shares, including the ones it can't
        index. {totalExcerpts.toLocaleString()} excerpts are searchable. Questions are answered first; indexing
        pauses while an answer is being written.
      </p>

      <div className="status-chips" role="group" aria-label="Filter by status">
        <button className={status === "all" ? "chip active" : "chip"} onClick={() => setStatus("all")}>
          All <span className="count">{scoped.length.toLocaleString()}</span>
        </button>
        {STATUS_ORDER.map((s) => (
          <button
            key={s}
            className={status === s ? `chip active ${s}` : `chip ${s}`}
            onClick={() => setStatus(status === s ? "all" : s)}
            disabled={!counts[s]}
            aria-pressed={status === s}
          >
            {STATUS_LABEL[s]} <span className="count">{(counts[s] ?? 0).toLocaleString()}</span>
          </button>
        ))}
      </div>

      <div className="filters">
        <input
          type="search"
          placeholder="Filter by name or folder…"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setLimit(PAGE);
          }}
          aria-label="Filter by name or folder"
        />
        <select
          value={source}
          onChange={(e) => {
            setSource(e.target.value);
            setLimit(PAGE);
          }}
          aria-label="Source"
        >
          <option value="all">All sources</option>
          {sources.map((s) => (
            <option key={s.id} value={String(s.id)}>
              {s.name}
              {s.active ? "" : " (no longer configured)"}
            </option>
          ))}
          <option value="uploads">Uploaded</option>
        </select>
        {sources.map((s) =>
          source === String(s.id) ? (
            <span key={s.id} className="meta small">
              {s.display_path} · checked {when(s.last_scan_at) || "never"}
              <button className="link" onClick={() => api.scanSource(s.id).then(refreshShares)}>
                Scan now
              </button>
            </span>
          ) : null,
        )}
      </div>

      <div className="bulk" aria-live="polite">
        {selectedDocs.length > 0 ? (
          <>
            <span>{selectedDocs.length} selected</span>
            {bulk.map((a) => {
              const n = selectedDocs.filter((d) => actionsFor(d).includes(a)).length;
              return (
                <button
                  key={a}
                  className={a === "exclude" ? "danger" : undefined}
                  disabled={!n || working}
                  onClick={() => run(a, selectedDocs)}
                >
                  {actionLabel(a)} ({n})
                </button>
              );
            })}
            <button className="link" onClick={() => setSelected(new Set())}>
              Clear selection
            </button>
          </>
        ) : (
          <span className="meta small">Select files to re-index, exclude or include several at once.</span>
        )}
      </div>
      {message && <p className="note">{message}</p>}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th className="sel">
                <input
                  type="checkbox"
                  aria-label="Select all listed files"
                  checked={allSelected}
                  disabled={selectable.length === 0}
                  onChange={(e) => {
                    const next = new Set(selected);
                    for (const id of selectable) {
                      if (e.target.checked) next.add(id);
                      else next.delete(id);
                    }
                    setSelected(next);
                  }}
                />
              </th>
              {header("path", "File")}
              <th>Source</th>
              {header("status", "Status")}
              {header("excerpts", "Excerpts")}
              {header("size", "Size")}
              {header("updated", "Indexed")}
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {loaded && visible.length === 0 && (
              <tr>
                <td colSpan={8} className="meta">
                  No files match.
                </td>
              </tr>
            )}
            {visible.slice(0, limit).map((r) => {
              const slash = r.path.lastIndexOf("/", r.path.length - 2);
              const folder = slash >= 0 ? r.path.slice(0, slash) : "";
              const name = slash >= 0 ? r.path.slice(slash + 1) : r.path;
              const d = r.kind === "doc" ? r.doc : null;
              return (
                <tr key={r.key} className={r.kind === "skip" || d?.status === "excluded" ? "muted" : undefined}>
                  <td className="sel">
                    {d?.can_manage && (
                      <input
                        type="checkbox"
                        aria-label={`Select ${name}`}
                        checked={selected.has(d.id)}
                        onChange={(e) => toggle(d.id, e.target.checked)}
                      />
                    )}
                  </td>
                  <td className="file">
                    {d ? (
                      <a href={api.fileUrl(d.id)} target="_blank" rel="noreferrer">
                        {name}
                      </a>
                    ) : (
                      <span>{name}</span>
                    )}
                    {folder && <div className="meta small">{folder}</div>}
                  </td>
                  <td className="meta">{r.source === null ? "Uploaded" : (sourceName.get(r.source) ?? "")}</td>
                  <td>
                    <StatusCell row={r} />
                  </td>
                  <td className="num">{d && d.status === "ready" ? d.chunks.toLocaleString() : ""}</td>
                  <td className="num meta">{size(d ? d.size_bytes : r.kind === "skip" ? r.file.size_bytes : null)}</td>
                  <td className="meta">{d ? when(d.indexed_at) : ""}</td>
                  <td className="actions">
                    {d &&
                      actionsFor(d).map((a) => (
                        <button
                          key={a}
                          className={a === "exclude" ? "link danger" : "link"}
                          disabled={working}
                          onClick={() => run(a, [d])}
                        >
                          {actionLabel(a, d)}
                        </button>
                      ))}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {visible.length > limit && (
        <p>
          <button onClick={() => setLimit(limit + PAGE)}>
            Show more ({(visible.length - limit).toLocaleString()} not shown)
          </button>
        </p>
      )}
    </section>
  );
}
