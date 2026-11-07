import { useEffect, useState } from "react";
import { api, ApiError, type Me } from "./api";
import Account from "./pages/Account";
import Admin from "./pages/Admin";
import Chat, { type PendingAsk } from "./pages/Chat";
import Documents from "./pages/Documents";
import Login from "./pages/Login";

type Tab = "ask" | "documents" | "admin" | "account";

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [checked, setChecked] = useState(false);
  const [tab, setTab] = useState<Tab>("ask");
  const [pending, setPending] = useState<PendingAsk | null>(null);

  useEffect(() => {
    api
      .me()
      .then(setMe)
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401)) console.error(e);
      })
      .finally(() => setChecked(true));
  }, []);

  if (!checked) return null;
  if (!me) return <Login onLogin={setMe} />;

  const signOut = async () => {
    await api.logout().catch(() => undefined);
    setMe(null);
    setTab("ask");
  };

  const tabs: [Tab, string][] = [
    ["ask", "Ask"],
    ["documents", "Documents"],
    ...(me.is_admin ? ([["admin", "Admin"]] as [Tab, string][]) : []),
    ["account", "Account"],
  ];

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">Document Assistant</span>
        <nav aria-label="Main">
          {tabs.map(([id, label]) => (
            <button
              key={id}
              className={tab === id ? "tab active" : "tab"}
              aria-current={tab === id ? "page" : undefined}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
        </nav>
        <span className="who">
          {me.display_name || me.username}
          <button className="link" onClick={signOut}>
            Sign out
          </button>
        </span>
      </header>
      <main>
        {/* Chat stays mounted so the conversation survives tab switches. */}
        <div hidden={tab !== "ask"}>
          <Chat pending={pending} onPendingTaken={() => setPending(null)} />
        </div>
        {tab === "documents" && (
          <Documents
            me={me}
            onSummarise={(doc) => {
              setPending({
                question: `Summarise ${doc.filename}`,
                options: { mode: "summary", document_ids: [doc.id] },
              });
              setTab("ask");
            }}
          />
        )}
        {tab === "admin" && me.is_admin && <Admin me={me} />}
        {tab === "account" && <Account me={me} />}
      </main>
    </div>
  );
}
