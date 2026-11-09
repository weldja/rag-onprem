import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, askStream, type AskOptions, type HistoryTurn, type Source } from "../api";
import type { Scope } from "../Sidebar";

interface Turn {
  id: number;
  question: string;
  answer: string;
  stage: "retrieving" | "generating" | "done" | "error";
  found: boolean;
  sources: Source[];
  summary: boolean;
  scope: string | null; // label of the folder/document the question was limited to
  startedAt: number;
  finishedAt: number | null;
  excerpts: number | null; // what the model is reading, from the "generating" status
  documents: string[];
  cached: boolean;
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

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** Seconds since `since`, ticking; hidden for the first two seconds. */
function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const s = Math.floor((now - since) / 1000);
  return s >= 2 ? <span className="elapsed">{s} s</span> : null;
}

/** Shown from the moment a question is sent until the first words arrive. */
function Thinking({ turn }: { turn: Turn }) {
  let label = "Searching your documents…";
  if (turn.stage === "generating") {
    label = turn.excerpts
      ? `Reading ${plural(turn.excerpts, "excerpt")} from ${plural(turn.documents.length, "document")}…`
      : "Writing an answer…";
  }
  const names = turn.documents.slice(0, 4).join(", ");
  const more = turn.documents.length > 4 ? ` and ${turn.documents.length - 4} more` : "";
  return (
    <div className="thinking" role="status" aria-live="polite">
      <div className="thinking-line">
        <span className="dots" aria-hidden>
          <i />
          <i />
          <i />
        </span>
        <span>{label}</span>
        <Elapsed since={turn.startedAt} />
      </div>
      {turn.stage === "generating" && names && (
        <p className="thinking-docs" title={turn.documents.join("\n")}>
          {names}
          {more}
        </p>
      )}
    </div>
  );
}

function Answer({ turn }: { turn: Turn }) {
  const [active, setActive] = useState<number | null>(null);
  const byId = new Map(turn.sources.map((s) => [s.id, s]));
  const waiting = turn.stage === "retrieving" || (turn.stage === "generating" && !turn.answer);
  const streaming = turn.stage === "generating" && !!turn.answer;
  const seconds = turn.finishedAt ? ((turn.finishedAt - turn.startedAt) / 1000).toFixed(1) : null;

  return (
    <div className={turn.found || turn.stage !== "done" ? "answer" : "answer muted"} aria-busy={waiting || streaming}>
      {waiting && <Thinking turn={turn} />}
      {turn.answer && (
        <div className={streaming ? "answer-text streaming" : "answer-text"}>
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
        </div>
      )}
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
      {turn.stage === "done" && seconds && (
        <p className="answer-meta">
          {turn.cached ? `Answered in ${seconds} s (asked before)` : `Answered in ${seconds} s`}
        </p>
      )}
    </div>
  );
}

export default function Chat({
  pending,
  onPendingTaken,
  scope,
  onClearScope,
}: {
  pending?: PendingAsk | null;
  onPendingTaken?: () => void;
  scope?: Scope | null;
  onClearScope?: () => void;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const nextId = useRef(1);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true); // follow the answer while the user is at the bottom

  const update = (id: number, patch: Partial<Turn> | ((t: Turn) => Partial<Turn>)) =>
    setTurns((all) =>
      all.map((t) => (t.id === id ? { ...t, ...(typeof patch === "function" ? patch(t) : patch) } : t)),
    );

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [turns]);

  const onScroll = () => {
    const el = scroller.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const run = async (q: string, options: AskOptions, scopeLabel: string | null = null) => {
    const id = nextId.current++;
    const summary = options.mode === "summary";
    stick.current = true;
    setTurns((all) => [
      ...all,
      {
        id,
        question: q,
        answer: "",
        stage: "retrieving",
        found: true,
        sources: [],
        summary,
        scope: scopeLabel,
        startedAt: Date.now(),
        finishedAt: null,
        excerpts: null,
        documents: [],
        cached: false,
      },
    ]);
    setBusy(true);
    abort.current = new AbortController();
    try {
      await askStream(
        q,
        options,
        (ev) => {
          if (ev.type === "status" && ev.stage === "generating")
            update(id, {
              stage: "generating",
              excerpts: ev.excerpts ?? null,
              documents: ev.documents ?? [],
              cached: !!ev.cached,
            });
          else if (ev.type === "status") update(id, { stage: ev.stage });
          else if (ev.type === "token") update(id, (t) => ({ answer: t.answer + ev.text }));
          else if (ev.type === "done")
            update(id, {
              answer: ev.answer,
              found: ev.found,
              sources: ev.sources,
              stage: "done",
              finishedAt: Date.now(),
            });
          else if (ev.type === "error") update(id, { answer: ev.message, stage: "error", found: false });
        },
        abort.current.signal,
      );
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        update(id, (t) => ({
          stage: "done",
          found: !!t.answer,
          answer: t.answer ? `${t.answer}\n\n*(stopped)*` : "Stopped.",
        }));
      } else {
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
    const options: AskOptions = { history: historyOf(turns) };
    if (scope) options.document_ids = scope.ids;
    await run(q, options, scope?.label ?? null);
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
      <div className="chat-scroll" ref={scroller} onScroll={onScroll}>
        <div className="chat-inner">
          {turns.length === 0 && (
            <div className="empty">
              <h2>Ask a question about your documents</h2>
              <p>Answers come only from documents you have access to, with numbered references.</p>
              <p className="meta">Pick a folder or document on the left to ask about just that.</p>
            </div>
          )}
          {turns.map((t) => (
            <article key={t.id} className="turn">
              <p className="question">
                {t.scope && <span className="question-scope">In {t.scope}</span>}
                {t.question}
              </p>
              <Answer turn={t} />
            </article>
          ))}
        </div>
      </div>
      <form className="composer" onSubmit={ask}>
        {scope && (
          <div className="scope-chip">
            <span>
              Answering from <strong title={scope.label}>{scope.label}</strong> (
              {scope.ids.length === 1 ? "1 document" : `${scope.ids.length} documents`})
            </span>
            <button type="button" className="link" onClick={onClearScope} aria-label="Ask about all documents again">
              ✕
            </button>
          </div>
        )}
        <div className="composer-row">
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
        </div>
      </form>
    </section>
  );
}
