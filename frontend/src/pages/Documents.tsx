import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, type DocumentInfo, type Group, type Me } from "../api";

function GroupPicker({
  groups,
  selected,
  onChange,
}: {
  groups: Group[];
  selected: number[];
  onChange: (ids: number[]) => void;
}) {
  if (groups.length === 0) return <span className="meta">No groups available</span>;
  return (
    <span className="picker">
      {groups.map((g) => (
        <label key={g.id} className="check">
          <input
            type="checkbox"
            checked={selected.includes(g.id)}
            onChange={(e) =>
              onChange(e.target.checked ? [...selected, g.id] : selected.filter((x) => x !== g.id))
            }
          />
          {g.name}
        </label>
      ))}
    </span>
  );
}

function size(bytes: number | null): string {
  if (bytes === null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

const BUSY = new Set(["pending", "processing"]);

export default function Documents({
  me,
  onSummarise,
}: {
  me: Me;
  onSummarise: (doc: DocumentInfo) => void;
}) {
  const [docs, setDocs] = useState<DocumentInfo[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [shareWith, setShareWith] = useState<number[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [editing, setEditing] = useState<{ id: number; groups: number[] } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  // Non-admins can only share with their own groups.
  const shareable = me.is_admin ? groups : groups.filter((g) => me.groups.some((m) => m.id === g.id));
  const names = new Map(groups.map((g) => [g.id, g.name]));

  const refresh = useCallback(() => api.documents().then(setDocs).catch((e) => setError(e.message)), []);

  useEffect(() => {
    void refresh();
    api.groups().then((all) => {
      setGroups(all);
      const allStaff = all.find((g) => g.name === "All staff");
      if (allStaff && (me.is_admin || me.groups.some((g) => g.id === allStaff.id))) {
        setShareWith([allStaff.id]);
      }
    });
  }, [refresh, me]);

  // Poll while anything is still being indexed.
  useEffect(() => {
    if (!docs.some((d) => BUSY.has(d.status))) return;
    const timer = setInterval(() => void refresh(), 3000);
    return () => clearInterval(timer);
  }, [docs, refresh]);

  const upload = async (e: FormEvent) => {
    e.preventDefault();
    const files = Array.from(fileInput.current?.files ?? []);
    if (files.length === 0) return;
    setUploading(true);
    setError("");
    setMessage("");
    const notes: string[] = [];
    for (const file of files) {
      try {
        const res = await api.upload(file, shareWith);
        notes.push(res.duplicate ? `${file.name}: already uploaded` : `${file.name}: queued for indexing`);
      } catch (err) {
        notes.push(`${file.name}: ${err instanceof Error ? err.message : "failed"}`);
      }
    }
    setMessage(notes.join(" · "));
    if (fileInput.current) fileInput.current.value = "";
    setUploading(false);
    void refresh();
  };

  const act = async (fn: () => Promise<unknown>, confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return;
    setError("");
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    }
    void refresh();
  };

  return (
    <section className="page">
      <h2>Documents</h2>
      <form className="card upload" onSubmit={upload}>
        <input
          ref={fileInput}
          type="file"
          multiple
          accept=".pdf,.docx,.pptx,.xlsx,.csv,.html,.htm,.txt,.md,.png,.jpg,.jpeg,.tif,.tiff"
          aria-label="Files to upload"
        />
        <div>
          <span className="label">Share with:</span>
          <GroupPicker groups={shareable} selected={shareWith} onChange={setShareWith} />
          {shareWith.length === 0 && <span className="meta"> (only you and administrators)</span>}
        </div>
        <button className="primary" disabled={uploading}>
          {uploading ? "Uploading…" : "Upload"}
        </button>
        {message && <p className="note">{message}</p>}
      </form>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <table className="table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Status</th>
            <th>Size</th>
            <th>Shared with</th>
            <th aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {docs.length === 0 && (
            <tr>
              <td colSpan={5} className="meta">
                No documents yet.
              </td>
            </tr>
          )}
          {docs.map((d) => (
            <tr key={d.id}>
              <td>
                <a href={api.fileUrl(d.id)} target="_blank" rel="noreferrer">
                  {d.filename}
                </a>
                {d.source_path && d.source_path !== d.filename && (
                  <div className="meta small" title="Indexed from a file share">
                    {d.source_path}
                  </div>
                )}
              </td>
              <td>
                <span className={`badge ${d.status}`}>{d.status}</span>
                {d.status === "ready" && <span className="meta"> {d.chunks} chunks</span>}
                {d.stale && <span className="badge failed">needs re-index</span>}
                {d.error && <div className="error small">{d.error}</div>}
              </td>
              <td className="meta">{size(d.size_bytes)}</td>
              <td>
                {editing?.id === d.id ? (
                  <span>
                    <GroupPicker
                      groups={shareable}
                      selected={editing.groups}
                      onChange={(ids) => setEditing({ id: d.id, groups: ids })}
                    />
                    <button
                      onClick={() =>
                        act(async () => {
                          await api.share(d.id, editing.groups);
                          setEditing(null);
                        })
                      }
                    >
                      Save
                    </button>
                    <button className="link" onClick={() => setEditing(null)}>
                      Cancel
                    </button>
                  </span>
                ) : d.group_ids.length ? (
                  d.group_ids.map((g) => names.get(g) ?? `#${g}`).join(", ")
                ) : (
                  <span className="meta">Private</span>
                )}
              </td>
              <td className="actions">
                {d.status === "ready" && editing?.id !== d.id && (
                  <button className="link" onClick={() => onSummarise(d)}>
                    Summarise
                  </button>
                )}
                {d.can_manage && editing?.id !== d.id && (
                  <>
                    {d.source_id === null && (
                      <button className="link" onClick={() => setEditing({ id: d.id, groups: d.group_ids })}>
                        Share
                      </button>
                    )}
                    <button
                      className="link"
                      disabled={BUSY.has(d.status)}
                      onClick={() => act(() => api.reindex(d.id))}
                    >
                      Re-index
                    </button>
                    {d.source_id === null && (
                      <button
                        className="link danger"
                        onClick={() => act(() => api.deleteDocument(d.id), `Delete ${d.filename}?`)}
                      >
                        Delete
                      </button>
                    )}
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
