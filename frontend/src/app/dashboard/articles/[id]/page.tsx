'use client';

import { useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import * as api from '@/lib/api';

export default function ArticleDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [article, setArticle] = useState<api.Article | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    setArticle(null); setError('');
    api.articles.get(id).then((a) => { if (active) setArticle(a); })
      .catch((e) => { if (active) setError(e.message || 'Could not load catalog article.'); });
    return () => { active = false; };
  }, [id]);
  return <div className="mx-auto max-w-3xl space-y-5">
    <Link href="/dashboard/articles" className="text-sm text-accent-deep hover:underline">← All catalog articles</Link>
    {error ? <p role="alert" className="text-red-700">{error}</p> : !article ? <p role="status">Loading catalog article…</p> : <section className="lio-card p-6">
      <p className="text-sm text-ink/50">Catalog article · {article.article_number}</p>
      <h1 className="mt-2 text-2xl font-semibold">{article.description}</h1>
      <dl className="mt-6 grid grid-cols-2 gap-5">
        <div><dt className="text-sm text-ink/50">Supplier</dt><dd className="mt-1">{article.supplier_name || 'Not specified'}</dd></div>
        <div><dt className="text-sm text-ink/50">Article number</dt><dd className="mt-1">{article.article_number}</dd></div>
        <div><dt className="text-sm text-ink/50">Catalog unit price</dt><dd className="mt-1 font-semibold">{article.currency} {Number(article.unit_price).toFixed(2)} / {article.unit}</dd></div>
        <div><dt className="text-sm text-ink/50">Reference quantity</dt><dd className="mt-1">{Number(article.quantity)} {article.unit}</dd></div>
      </dl><p className="mt-6 text-sm text-ink/50">Prices exclude tax. Opened from a request? Return to its tab to continue editing.</p>
    </section>}
  </div>;
}
