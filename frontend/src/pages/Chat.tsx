import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, askStream, type AskOptions, type HistoryTurn, type Source } from "../api";

interface Turn {
  id: number;
  question: string;
  answer: string;
  stage: "retrieving" | "generating" | "done" | "error";
  found: boolean;
  sources: Source[];
  summary: boolean;
}

/** A request handed over from another page (e.g. "Summarise" on Documents). */
export interface PendingAsk {
  question: string;
  options: AskOptions;
}

// Previous exchanges sent with each question so follow-ups make sense. The
// server uses only the most recent ones; the conversation lives in this tab.
const HISTORY_TURNS = 3;

function historyOf(turns: Turn[]): HistoryTurn[] {
  return turns
    .filter((t) => t.stage === "done" && t.found && !t.summary)
    .slice(-HISTORY_TURNS)
    .flatMap((t) => [
      { role: "user" as const, content: t.question },
      { role: "assistant" as const, content: t.answer },
    ]);
}

// Turn [n] markers into in-page links so they can be rendered as citation buttons.
function linkCitations(text: string): string {
  return text.replace(/\[(\d+)\](?!\()/g, "[\\[$1\\]](#cite-$1)");
}

function Answer({ turn }: { turn: Turn }) {
  const [active, setActive] = useState<number | null>(null);
  const byId = new Map(turn.sources.map((s) => [s.id, s]));

  return (
    <div className={turn.found || turn.stage !== "done" ? "answer" : "answer muted"}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            const m = href?.match(/^#cite-(\d+)$/);
            if (m && byId.has(Number(m[1]))) {
              const n = Number(m[1]);
              return (
                <button
                  className={active === n ? "cite active" : "cite"}
                  onClick={() => setActive(active === n ? null : n)}
                  title={byId.get(n)!.document}
                >
                  {n}
                </button>
              );
            }
            // Answers come from a model reading customer files: never render
            // links (or images) that could send data anywhere.
            return <span>{children}</span>;
          },
          img: () => null,
        }}
      >
        {linkCitations(turn.answer)}
      </ReactMarkdown>
      {turn.stage === "retrieving" && <p className="status">Searching your documents…</p>}
      {turn.stage === "generating" && !turn.answer && <p className="status">Writing an answer…</p>}
      {turn.sources.length > 0 && (
        <ol className="sources">
          {turn.sources.map((s) => (
            <li
              key={s.id}
              className={active === s.id ? "source active" : "source"}
              onClick={() => setActive(s.id)}
            >
              <div className="source-head">
                <span className="cite static">{s.id}</span>
                <a href={api.fileUrl(s.document_id, s.page)} target="_blank" rel="noreferrer">
                  {s.document}
                </a>
                {s.page !== null && <span className="meta">page {s.page}</span>}
                {s.section && <span className="meta">{s.section}</span>}
              </div>
              <p className="snippet">{s.snippet}</p>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

export default function Chat({
  pending,
  onPendingTaken,
}: {
  pending?: PendingAsk | null;
  onPendingTaken?: () => void;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const nextId = useRef(1);

  const update = (id: number, patch: Partial<Turn> | ((t: Turn) => Partial<Turn>)) =>
    setTurns((all) =>
      all.map((t) => (t.id === id ? { ...t, ...(typeof patch === "function" ? patch(t) : patch) } : t)),
    );

  const run = async (q: string, options: AskOptions) => {
    const id = nextId.current++;
    const summary = options.mode === "summary";
    setTurns((all) => [
      ...all,
      { id, question: q, answer: "", stage: "retrieving", found: true, sources: [], summary },
    ]);
    setBusy(true);
    abort.current = new AbortController();
    try {
      await askStream(
        q,
        options,
        (ev) => {
          if (ev.type === "status") update(id, { stage: ev.stage });
          else if (ev.type === "token") update(id, (t) => ({ answer: t.answer + ev.text }));
          else if (ev.type === "done")
            update(id, { answer: ev.answer, found: ev.found, sources: ev.sources, stage: "done" });
          else if (ev.type === "error") update(id, { answer: ev.message, stage: "error", found: false });
        },
        abort.current.signal,
      );
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        update(id, {
          answer: err instanceof Error ? err.message : "Something went wrong",
          stage: "error",
          found: false,
        });
      }
    } finally {
      setBusy(false);
      abort.current = null;
    }
  };

  const ask = async (e?: FormEvent) => {
    e?.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    setQuestion("");
    await run(q, { history: historyOf(turns) });
  };

  // Requests handed over from other pages run as soon as nothing else is.
  useEffect(() => {
    if (pending && !busy) {
      onPendingTaken?.();
      void run(pending.question, pending.options);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending, busy]);

  const newConversation = () => {
    abort.current?.abort();
    setTurns([]);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void ask();
    }
  };

  return (
    <section className="chat">
      {turns.length === 0 && (
        <div className="empty">
          <h2>Ask a question about your documents</h2>
          <p>Answers come only from documents you have access to, with numbered references.</p>
        </div>
      )}
      {turns.map((t) => (
        <article key={t.id} className="turn">
          <p className="question">{t.question}</p>
          <Answer turn={t} />
        </article>
      ))}
      <form className="composer" onSubmit={ask}>
        {turns.length > 0 && (
          <button type="button" className="link" onClick={newConversation} title="Forget this conversation">
            New conversation
          </button>
        )}
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={onKey}
          placeholder="Ask a question…  (Enter to send, Shift+Enter for a new line)"
          maxLength={2000}
          rows={2}
          aria-label="Question"
        />
        {busy ? (
          <button type="button" onClick={() => abort.current?.abort()}>
            Stop
          </button>
        ) : (
          <button className="primary" disabled={!question.trim()}>
            Ask
          </button>
        )}
      </form>
    </section>
  );
}
