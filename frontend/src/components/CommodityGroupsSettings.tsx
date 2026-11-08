'use client';

import { useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import * as api from '@/lib/api';
import { Alert, Button, Input } from '@/components/base';
import type { CommodityGroup } from '@/types/database';
import { cn } from '@/lib/utils';

type Draft = { id: number | null; category: string; name: string; description: string };

const EMPTY: Draft = { id: null, category: '', name: '', description: '' };

/** Admin tab: the organization's commodity groups and their booking descriptions. */
export default function CommodityGroupsSettings() {
  const queryClient = useQueryClient();
  const [groups, setGroups] = useState<CommodityGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState('');
  const [showArchived, setShowArchived] = useState(false);
  const [editing, setEditing] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = async () => {
    try {
      setGroups(await api.commodityGroups.list(true));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load commodity groups');
    }
    setLoading(false);
  };

  useEffect(() => {
    load();
  }, []);

  /** Reload this list and every picker that caches groups. */
  const refresh = async () => {
    await load();
    await queryClient.invalidateQueries({ queryKey: ['commodity-groups'] });
  };

  const categories = useMemo(
    () => [...new Set(groups.map((g) => g.category))].sort((a, b) => a.localeCompare(b)),
    [groups],
  );

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return groups.filter(
      (g) =>
        (showArchived || !g.archived) &&
        (!q || `${g.category} ${g.name} ${g.description ?? ''}`.toLowerCase().includes(q)),
    );
  }, [groups, query, showArchived]);

  const byCategory = useMemo(() => {
    const map = new Map<string, CommodityGroup[]>();
    for (const g of visible) map.set(g.category, [...(map.get(g.category) ?? []), g]);
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [visible]);

  const run = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await fn();
      setNotice(done);
      await refresh();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong');
      return false;
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!editing) return;
    const input = {
      category: editing.category.trim(),
      name: editing.name.trim(),
      description: editing.description.trim() || null,
    };
    if (!input.category || !input.name) {
      setError('Category and name are required');
      return;
    }
    const ok = await run(
      () => (editing.id ? api.commodityGroups.update(editing.id, input) : api.commodityGroups.create(input)),
      editing.id ? `Saved “${input.name}”.` : `Added “${input.name}”.`,
    );
    if (ok) setEditing(null);
  };

  const remove = async (g: CommodityGroup) => {
    if (!confirm(`Delete “${g.name}”? This cannot be undone.`)) return;
    await run(() => api.commodityGroups.delete(g.id), `Deleted “${g.name}”.`);
  };

  return (
    <div className="lio-card p-6 mb-6">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-xl font-semibold tracking-tight text-ink">Commodity groups</h2>
        <Button size="sm" onClick={() => setEditing({ ...EMPTY })} disabled={busy}>
          Add group
        </Button>
      </div>
      <p className="mb-4 text-sm text-ink/55">
        Groups used to book order lines. The description tells the classifier what belongs in a group,
        including your own booking rules.
      </p>

      {error && <Alert variant="error" className="mb-4">{error}</Alert>}
      {notice && <Alert variant="success" className="mb-4">{notice}</Alert>}

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search groups"
          className="h-10 min-w-0 flex-1 rounded-xl border border-line bg-white px-3.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent-deep/60"
        />
        <label className="flex items-center gap-2 text-sm text-ink/70">
          <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
          Show archived
        </label>
      </div>

      {loading ? (
        <p className="py-6 text-center text-sm text-ink/45">Loading…</p>
      ) : byCategory.length === 0 ? (
        <p className="py-6 text-center text-sm text-ink/45">No groups match.</p>
      ) : (
        <div className="space-y-5">
          {byCategory.map(([category, items]) => (
            <section key={category}>
              <h3 className="mb-2 text-sm font-medium text-ink/55">{category}</h3>
              <ul className="space-y-1.5">
                {items.map((g) => (
                  <li
                    key={g.id}
                    className={cn(
                      'flex items-start justify-between gap-3 rounded-lg bg-ink-800/[0.02] p-3',
                      g.archived && 'opacity-60',
                    )}
                  >
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-ink">
                        {g.name}
                        {g.archived && (
                          <span className="ml-2 rounded-full bg-ink-800/[0.06] px-2 py-px text-xs font-medium text-ink/55">
                            Archived
                          </span>
                        )}
                      </p>
                      {g.description && (
                        <p className="mt-0.5 line-clamp-2 text-sm text-ink/55" title={g.description}>
                          {g.description}
                        </p>
                      )}
                    </div>
                    <div className="flex shrink-0 items-center gap-3 text-sm">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          setEditing({ id: g.id, category: g.category, name: g.name, description: g.description ?? '' })
                        }
                        className="font-medium text-accent-deep hover:underline"
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          run(
                            () => (g.archived ? api.commodityGroups.unarchive(g.id) : api.commodityGroups.archive(g.id)),
                            g.archived ? `Restored “${g.name}”.` : `Archived “${g.name}”.`,
                          )
                        }
                        className="font-medium text-ink/60 hover:text-ink"
                      >
                        {g.archived ? 'Unarchive' : 'Archive'}
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => remove(g)}
                        title="Only groups no request uses can be deleted; archive the others."
                        className="font-medium text-red-600 hover:text-red-700"
                      >
                        Delete
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}

      {editing && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={() => !busy && setEditing(null)}
        >
          <div className="w-full max-w-lg rounded-2xl bg-white shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="border-b border-line p-6">
              <h3 className="text-xl font-semibold tracking-tight text-ink">
                {editing.id ? 'Edit commodity group' : 'Add commodity group'}
              </h3>
            </div>
            <div className="space-y-4 p-6">
              <Input
                label="Category"
                list="commodity-categories"
                value={editing.category}
                onChange={(e) => setEditing({ ...editing, category: e.target.value })}
                helperText="Pick an existing category or type a new one."
              />
              <datalist id="commodity-categories">
                {categories.map((c) => (
                  <option key={c} value={c} />
                ))}
              </datalist>
              <Input
                label="Name"
                value={editing.name}
                onChange={(e) => setEditing({ ...editing, name: e.target.value })}
              />
              <label className="block">
                <span className="mb-1.5 block text-sm font-medium text-ink/70">Description</span>
                <textarea
                  rows={4}
                  value={editing.description}
                  onChange={(e) => setEditing({ ...editing, description: e.target.value })}
                  className="w-full rounded-xl border border-line bg-white px-3.5 py-2.5 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent-deep/60"
                />
                <span className="mt-1.5 block text-sm text-ink/45">
                  Tell the classifier what belongs here, including your own booking rules, e.g. “Always book
                  cable ties here.”
                </span>
              </label>
            </div>
            <div className="flex gap-3 border-t border-line bg-ink-800/[0.02] p-6">
              <Button onClick={save} loading={busy} disabled={busy} className="flex-1">
                Save
              </Button>
              <Button variant="outline" onClick={() => setEditing(null)} disabled={busy} className="flex-1">
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
