import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type Group, type Me, type User } from "../api";

export default function Admin({ me }: { me: Me }) {
  const [users, setUsers] = useState<User[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [error, setError] = useState("");
  const [newGroup, setNewGroup] = useState("");
  const [draft, setDraft] = useState({
    username: "",
    display_name: "",
    password: "",
    is_admin: false,
    group_ids: [] as number[],
  });

  const refresh = useCallback(async () => {
    try {
      const [u, g] = await Promise.all([api.users(), api.adminGroups()]);
      setUsers(u);
      setGroups(g);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const run = async (fn: () => Promise<unknown>, confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return false;
    setError("");
    try {
      await fn();
      await refresh();
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
      return false;
    }
  };

  const createUser = async (e: FormEvent) => {
    e.preventDefault();
    const ok = await run(() =>
      api.createUser({ ...draft, display_name: draft.display_name || undefined }),
    );
    if (ok) setDraft({ username: "", display_name: "", password: "", is_admin: false, group_ids: [] });
  };

  const toggleGroup = (user: User, groupId: number, on: boolean) =>
    run(() =>
      api.updateUser(user.id, {
        group_ids: on ? [...user.group_ids, groupId] : user.group_ids.filter((g) => g !== groupId),
      }),
    );

  const resetPassword = (user: User) => {
    const password = window.prompt(`New password for ${user.username} (at least 12 characters):`);
    if (password) void run(() => api.updateUser(user.id, { password }));
  };

  return (
    <section className="page">
      <h2>Administration</h2>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <h3>Users</h3>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th>User</th>
              {groups.map((g) => (
                <th key={g.id}>{g.name}</th>
              ))}
              <th>Admin</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className={u.disabled ? "disabled" : undefined}>
                <td>
                  <strong>{u.username}</strong>
                  {u.display_name && <span className="meta"> {u.display_name}</span>}
                  {u.disabled && <span className="badge failed">disabled</span>}
                </td>
                {groups.map((g) => (
                  <td key={g.id}>
                    <input
                      type="checkbox"
                      aria-label={`${u.username} in ${g.name}`}
                      checked={u.group_ids.includes(g.id)}
                      onChange={(e) => toggleGroup(u, g.id, e.target.checked)}
                    />
                  </td>
                ))}
                <td>
                  <input
                    type="checkbox"
                    aria-label={`${u.username} is admin`}
                    checked={u.is_admin}
                    disabled={u.id === me.id}
                    onChange={(e) => run(() => api.updateUser(u.id, { is_admin: e.target.checked }))}
                  />
                </td>
                <td className="actions">
                  <button className="link" onClick={() => resetPassword(u)}>
                    Reset password
                  </button>
                  {u.id !== me.id && (
                    <>
                      <button
                        className="link"
                        onClick={() => run(() => api.updateUser(u.id, { disabled: !u.disabled }))}
                      >
                        {u.disabled ? "Enable" : "Disable"}
                      </button>
                      <button
                        className="link danger"
                        onClick={() => run(() => api.deleteUser(u.id), `Delete user ${u.username}?`)}
                      >
                        Delete
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <form className="card form-grid" onSubmit={createUser}>
        <h3>Add user</h3>
        <label>
          Username
          <input
            value={draft.username}
            onChange={(e) => setDraft({ ...draft, username: e.target.value })}
            required
            autoComplete="off"
          />
        </label>
        <label>
          Display name
          <input
            value={draft.display_name}
            onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
          />
        </label>
        <label>
          Initial password
          <input
            type="password"
            minLength={12}
            value={draft.password}
            onChange={(e) => setDraft({ ...draft, password: e.target.value })}
            required
            autoComplete="new-password"
          />
        </label>
        <div>
          <span className="label">Groups:</span>
          {groups.map((g) => (
            <label key={g.id} className="check">
              <input
                type="checkbox"
                checked={draft.group_ids.includes(g.id)}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    group_ids: e.target.checked
                      ? [...draft.group_ids, g.id]
                      : draft.group_ids.filter((x) => x !== g.id),
                  })
                }
              />
              {g.name}
            </label>
          ))}
        </div>
        <label className="check">
          <input
            type="checkbox"
            checked={draft.is_admin}
            onChange={(e) => setDraft({ ...draft, is_admin: e.target.checked })}
          />
          Administrator
        </label>
        <button className="primary">Add user</button>
      </form>

      <h3>Groups</h3>
      <ul className="group-list">
        {groups.map((g) => (
          <li key={g.id}>
            {g.name} <span className="meta">{g.members} members</span>
            <button
              className="link danger"
              onClick={() =>
                run(
                  () => api.deleteGroup(g.id),
                  `Delete group ${g.name}? Documents shared only with it become visible to their uploader and administrators only.`,
                )
              }
            >
              Delete
            </button>
          </li>
        ))}
      </ul>
      <form
        className="inline"
        onSubmit={async (e) => {
          e.preventDefault();
          if (await run(() => api.createGroup(newGroup))) setNewGroup("");
        }}
      >
        <input
          value={newGroup}
          onChange={(e) => setNewGroup(e.target.value)}
          placeholder="New group name"
          aria-label="New group name"
          required
        />
        <button>Add group</button>
      </form>
    </section>
  );
}
