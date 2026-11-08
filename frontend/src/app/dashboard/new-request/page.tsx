'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import * as api from '@/lib/api';
import { useCommodityGroups } from '@/lib/hooks/useCommodityGroups';
import { Button, Input, Alert, Loader } from '@/components/base';
import OrderLinesEditor from '@/components/OrderLinesEditor';
import SourceButton from '@/components/SourceButton';
import WarningList, { normalizeWarnings } from '@/components/WarningList';
import CatalogSuggestion, { applyArticle, restoreArticle } from '@/components/CatalogSuggestion';
import { useCatalogSuggestions } from '@/lib/useCatalogSuggestions';
import DocumentPanel, { type DocumentPanelHandle } from '@/components/DocumentPanel';
import {
  type Line,
  computeTotals,
  linesForSave,
  groupSources,
  missingGroupError,
  normalizeLines,
  primaryGroupId,
} from '@/lib/orderLines';

/** Everything a draft keeps (the PDF is stored separately as the draft's document). */
interface DraftData {
  requestorName: string;
  title: string;
  vendorName: string;
  vatId: string;
  quoteNumber?: string;
  validUntil?: string;
  department: string;
  orderLines: Line[];
  taxTotal: number;
  quotedGrandTotal: number | null;
  extractionWarnings: (api.WarningDetail | string)[];
  fieldSources: api.FieldSources;
  totalCost: number;
  /** Extraction finished for the draft's PDF (a draft saved mid-extraction resumes it). */
  extracted?: boolean;
}

type SaveState = { kind: 'idle' } | { kind: 'saving' } | { kind: 'saved'; at: string } | { kind: 'error' };

const isPdfFile = (f: File) => f.type === 'application/pdf' || /\.pdf$/i.test(f.name);

export default function NewRequestPage() {
  const router = useRouter();
  const { data: commodityGroups } = useCommodityGroups();

  // Form state
  const [requestorName, setRequestorName] = useState('');
  const [title, setTitle] = useState('');
  const [vendorName, setVendorName] = useState('');
  const [vatId, setVatId] = useState('');
  const [quoteNumber, setQuoteNumber] = useState('');
  const [extracted, setExtracted] = useState(false);
  const [validUntil, setValidUntil] = useState('');
  const [department, setDepartment] = useState('');
  const [orderLines, setOrderLines] = useState<Line[]>([]);
  const [taxTotal, setTaxTotal] = useState<number>(0);
  const [quotedGrandTotal, setQuotedGrandTotal] = useState<number | null>(null);
  const [extractionWarnings, setExtractionWarnings] = useState<api.WarningDetail[]>([]);
  const [fieldSources, setFieldSources] = useState<api.FieldSources>({});

  /** Label row with an eye button when the field's value is located in the PDF. */
  const fieldLabel = (text: string, key: keyof api.FieldSources) => {
    const boxes = fieldSources[key] ?? [];
    return (
      <div className="mb-1.5 flex h-5 items-center justify-between">
        <span className="text-sm font-medium text-ink/70">{text}</span>
        {fileUrl && boxes.length > 0 && (
          <SourceButton
            className="h-6 w-6 -my-0.5"
            label={`Show ${text.replace(' *', '').toLowerCase()} in document`}
            onClick={() => docPanel.current?.showSource({ sources: boxes })}
          />
        )}
      </div>
    );
  };
  const totals = computeTotals(orderLines, taxTotal);
  // Negotiated catalog prices for the lines (task 2).
  const { byKey: catalog, loading: catalogLoading } = useCatalogSuggestions(orderLines, true);
  const [appliedArticles, setAppliedArticles] = useState<Record<string, api.ArticleSuggestion>>({});
  const otherSuppliers = [
    ...new Set(
      orderLines
        .filter((l) => !l.removed && l.articleId && appliedArticles[l.articleId]?.supplierName)
        .map((l) => appliedArticles[l.articleId!].supplierName!)
        .filter((name) => name.trim().toLowerCase() !== vendorName.trim().toLowerCase()),
    ),
  ];
  // Groups are booked per line; the request shows the one with the largest spend.
  const primaryGroup = primaryGroupId(orderLines);
  const primaryGroupLabel = (() => {
    const g = commodityGroups?.find((x) => x.id === primaryGroup);
    return g ? `${g.category} · ${g.name}` : null;
  })();
  const primaryGroupBoxes = groupSources(orderLines, primaryGroup);

  // ---- Drafts (server-side, Gmail-style): load ?draft=<id>, autosave, delete on submit.
  const [draftId, setDraftId] = useState<string | null>(null);
  const draftIdRef = useRef<string | null>(null);
  const creatingDraft = useRef<Promise<string> | null>(null);
  const [draftDocUploaded, setDraftDocUploaded] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' });
  const submitted = useRef(false);
  const pendingDraftSave = useRef<Promise<unknown>>(Promise.resolve());
  const pendingDocument = useRef<Promise<unknown>>(Promise.resolve());
  const uploadedDocument = useRef<File | null>(null);
  const currentFile = useRef<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  /** The requestor the last extracted quote filled in, so a new quote can replace it. */
  const extractedRequestor = useRef('');

  const rememberDraftId = (id: string) => {
    draftIdRef.current = id;
    setDraftId(id);
    // Keep the URL shareable with yourself: a refresh reopens this draft.
    window.history.replaceState(null, '', `/dashboard/new-request?draft=${id}`);
  };

  useEffect(() => {
    (async () => {
      const id = new URLSearchParams(window.location.search).get('draft');
      if (id) {
        try {
          const d = await api.drafts.get<DraftData>(id);
          const data = d.data;
          setRequestorName(data.requestorName ?? '');
          setTitle(data.title ?? '');
          setVendorName(data.vendorName ?? '');
          setVatId(data.vatId ?? '');
          setQuoteNumber(data.quoteNumber ?? '');
          setValidUntil(data.validUntil ?? '');
          setDepartment(data.department ?? '');
          setOrderLines(normalizeLines(data.orderLines ?? []));
          setTaxTotal(data.taxTotal ?? 0);
          setQuotedGrandTotal(data.quotedGrandTotal ?? null);
          setExtractionWarnings(normalizeWarnings(data.extractionWarnings));
          setFieldSources(data.fieldSources ?? {});
          const wasExtracted = data.extracted ?? (data.orderLines?.length ?? 0) > 0;
          setExtracted(wasExtracted);
          rememberDraftId(id);
          let restored: File | null = null;
          if (d.hasDocument) {
            let url: string | null = null;
            try {
              url = await api.drafts.documentUrl(id);
              if (url) {
                const response = await fetch(url);
                if (!response.ok) throw new Error('Document download failed');
                const blob = await response.blob();
                restored = new File([blob], d.fileName ?? 'document.pdf', { type: 'application/pdf' });
                setFile(restored);
                uploadedDocument.current = restored;
                setDraftDocUploaded(true);
              }
            } catch {
              setError('Could not load the draft PDF. Your draft fields are preserved; reload to try again.');
            } finally {
              if (url) URL.revokeObjectURL(url);
            }
          }
          setSaveState({ kind: 'saved', at: d.updatedAt });
          // Left before extraction finished: pick it up again from the saved PDF.
          if (restored && !wasExtracted) void runExtraction(restored);
        } catch {
          setError('This draft no longer exists. Starting a new request.');
          window.history.replaceState(null, '', '/dashboard/new-request');
        }
      }
      setLoaded(true);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Draft id, creating the draft on first need (only one create in flight). */
  const ensureDraft = async (data: DraftData): Promise<string> => {
    if (draftIdRef.current) return draftIdRef.current;
    if (!creatingDraft.current) {
      creatingDraft.current = api.drafts.create(data).then((d) => {
        rememberDraftId(d.id);
        return d.id;
      });
      creatingDraft.current.catch(() => {
        creatingDraft.current = null;
      });
    }
    return creatingDraft.current;
  };

  // Document panel (side-by-side PDF with search-to-highlight)
  const docPanel = useRef<DocumentPanelHandle>(null);

  // UI state
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [warning, setWarning] = useState('');
  const [extracting, setExtracting] = useState(false);

  currentFile.current = file;
  const fileUrl = useMemo(() => (file ? URL.createObjectURL(file) : null), [file]);
  useEffect(() => () => { if (fileUrl) URL.revokeObjectURL(fileUrl); }, [fileUrl]);

  const draftData: DraftData = {
    requestorName, title, vendorName, vatId, quoteNumber, validUntil, department, orderLines,
    taxTotal, quotedGrandTotal, extractionWarnings, fieldSources, totalCost: totals.grand, extracted,
  };

  // Autosave ~1s after the last change, once there is something worth keeping.
  const draftJson = JSON.stringify(draftData);
  useEffect(() => {
    if (!loaded || submitted.current) return;
    // Saved as soon as any field has a value.
    const worthKeeping =
      !!file ||
      orderLines.length > 0 ||
      !!(requestorName || department || title || vendorName || vatId || quoteNumber || validUntil || taxTotal);
    if (!worthKeeping) return;
    const t = setTimeout(() => {
      if (submitted.current) return;
      // Serialize writes: a slower older save must never overwrite newer edits.
      pendingDraftSave.current = pendingDraftSave.current.catch(() => {}).then(async () => {
        if (submitted.current) return;
        setSaveState({ kind: 'saving' });
        try {
          const data = JSON.parse(draftJson) as DraftData;
          const id = await ensureDraft(data);
          const summary = await api.drafts.update(id, data);
          if (!submitted.current) setSaveState({ kind: 'saved', at: summary.updatedAt });
        } catch {
          if (!submitted.current) setSaveState({ kind: 'error' });
        }
      });
    }, 1000);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftJson, loaded, !!file]);

  // The PDF goes to the draft once (it is the draft's document from then on).
  useEffect(() => {
    if (!loaded || !file || draftDocUploaded || submitted.current) return;
    const uploadingFile = file;
    pendingDocument.current = pendingDocument.current.catch(() => {}).then(async () => {
      if (submitted.current || currentFile.current !== uploadingFile) return;
      try {
        const id = await ensureDraft(JSON.parse(draftJson) as DraftData);
        await api.drafts.uploadDocument(id, uploadingFile);
        if (currentFile.current === uploadingFile) {
          uploadedDocument.current = uploadingFile;
          setDraftDocUploaded(true);
        }
      } catch {
        if (!submitted.current) setSaveState({ kind: 'error' });
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file, loaded, draftDocUploaded]);

  const discardDraft = async () => {
    if (submitted.current) return;
    if (!confirm('Discard this draft? This cannot be undone.')) return;
    submitted.current = true;
    await Promise.allSettled([pendingDraftSave.current, pendingDocument.current, creatingDraft.current]);
    if (draftIdRef.current) await api.drafts.delete(draftIdRef.current).catch(() => {});
    router.push('/dashboard/requests');
  };

  // Handle file upload and extraction
  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (submitted.current) return;
    const uploadedFile = e.target.files?.[0];
    if (!uploadedFile) return;

    if (!isPdfFile(uploadedFile)) {
      setError('Please upload a PDF file');
      return;
    }

    setFile(uploadedFile);
    setDraftDocUploaded(false);
    // A failed replacement extraction must not leave the old quote's values
    // paired with the newly uploaded document.
    setOrderLines([]);
    setTitle(''); setVendorName(''); setVatId(''); setDepartment('');
    setQuoteNumber(''); setValidUntil(''); setTaxTotal(0); setQuotedGrandTotal(null);
    setAppliedArticles({});
    setError('');
    setSuccess('');
    setWarning('');
    setExtractionWarnings([]);
    setFieldSources({});
    setExtracted(false);
    await runExtraction(uploadedFile);
  };

  /** Extract a PDF into the form (also resumes a draft left mid-extraction). */
  const runExtraction = async (pdfFile: File) => {
    setExtracting(true);

    try {
      // One call: rows + boxes from the PDF, structured lines, classification.
      const extractionResult = await api.extraction.extractPdf(pdfFile);

      if (!extractionResult.success || !extractionResult.data) {
        throw new Error(extractionResult.error || 'Failed to extract data');
      }

      if (submitted.current) return;
      const extracted = extractionResult.data;

      // Step 3: Auto-fill form (including commodity group from extraction)
      // The quote's contact fills the requestor when it is empty or came from the
      // previous quote; a name the user typed stays.
      const quoteRequestor = extracted.requestorName || '';
      const previousQuoteRequestor = extractedRequestor.current;
      setRequestorName((current) =>
        current === '' || current === previousQuoteRequestor ? quoteRequestor : current,
      );
      extractedRequestor.current = quoteRequestor;
      setTitle(extracted.title || '');
      setVendorName(extracted.vendorName || '');
      setVatId(extracted.vatId || '');
      setQuoteNumber(extracted.quoteNumber || '');
      setValidUntil(extracted.validUntil || '');
      // A replacement quote must not keep the previous document's department.
      setDepartment(extracted.department || '');
      setOrderLines(normalizeLines(extracted.orderLines || []));
      setTaxTotal(extracted.taxTotal ?? 0);
      setQuotedGrandTotal(extracted.totalCost || null);
      setExtractionWarnings(
        normalizeWarnings(extractionResult.warningDetails ?? extractionResult.warnings),
      );
      setFieldSources(extracted.fieldSources ?? {});

      // Show warnings for missing fields
      const missing = extractionResult.missingFields ?? [];
      if (missing.length > 0) {
        setWarning(
          `⚠️ Could not extract: ${missing.join(', ')}. Please fill in these fields manually.`
        );
      }

      setExtracted(true);
      setSuccess('✓ PDF extracted successfully! Review and submit.');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (err) {
      if (submitted.current) return;
      console.error('Extraction error:', err);
      setError(err instanceof Error ? err.message : 'Failed to extract data from PDF');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } finally {
      setExtracting(false);
    }
  };

  // Submit form
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitted.current || extracting || !loaded) return;
    setError('');
    setSuccess('');
    setWarning('');

    // Validation with scroll to top on error
    const linesToSave = linesForSave(orderLines);
    if (linesToSave.length === 0) {
      setError('Please add at least one order line');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }

    // Spend is booked per line, so every line that counts needs its own group.
    const groupError = missingGroupError(linesToSave);
    if (groupError) {
      setError(groupError);
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }

    try {
      submitted.current = true;
      setSubmitting(true);
      // Finish writes before the server consumes/deletes the draft. This also
      // guarantees a replacement quote's PDF travels with its extracted lines.
      await Promise.allSettled([pendingDraftSave.current, pendingDocument.current, creatingDraft.current]);
      if (file && uploadedDocument.current !== file) {
        const id = await ensureDraft(draftData);
        await api.drafts.uploadDocument(id, file);
        uploadedDocument.current = file;
      }
      const created = await api.requests.create({
        draftId: draftIdRef.current,
        requestorName,
        titleShortDescription: title,
        vendorName,
        vatId,
        quoteNumber: quoteNumber || null,
        validUntil: validUntil || null,
        commodityGroupId: primaryGroup,
        totalCost: totals.grand,
        netTotal: totals.net,
        taxTotal,
        fieldSources,
        extractionWarnings,
        department,
        orderLines: linesToSave,
      });

      setSuccess('Request created successfully! Redirecting...');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      setTimeout(() => {
        router.push('/dashboard/requests');
        router.refresh();
      }, 1500);
    } catch (err) {
      submitted.current = false;
      setSubmitting(false);
      setError(err instanceof Error ? err.message : 'Failed to create request');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  };

  return (
    <div className={`${fileUrl ? 'max-w-[1600px]' : 'max-w-5xl'} mx-auto px-4 sm:px-6 lg:px-8 py-8`}>
      {/* Header */}
      <div className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-ink">New Procurement Request</h1>
          <p className="text-gray-600 mt-2">
            Upload a vendor offer PDF or fill in the details manually
          </p>
        </div>
        {(draftId || saveState.kind !== 'idle') && (
          <div className="flex items-center gap-4 pt-2 text-sm">
            <span className={saveState.kind === 'error' ? 'text-red-600' : 'text-ink/50'}>
              {saveState.kind === 'saving' && 'Saving draft…'}
              {saveState.kind === 'saved' &&
                `Draft saved ${new Date(saveState.at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}`}
              {saveState.kind === 'error' && 'Couldn’t save draft'}
            </span>
            {draftId && (
              <button type="button" onClick={discardDraft} disabled={submitting} className="font-medium text-red-600 hover:text-red-700">
                Discard draft
              </button>
            )}
          </div>
        )}
      </div>

      {error && <Alert variant="error" className="mb-6">{error}</Alert>}
      {warning && <Alert variant="warning" className="mb-6">{warning}</Alert>}
      {success && <Alert variant="success" className="mb-6">{success}</Alert>}

      <div className={fileUrl ? 'grid grid-cols-1 xl:grid-cols-2 gap-6 items-start' : ''}>
      <form onSubmit={handleSubmit} className="space-y-8 min-w-0">
        <fieldset disabled={submitting} className="space-y-8 min-w-0">
        {/* PDF Upload Section */}
        <div className="lio-card p-6">
          <h2 className="text-xl font-semibold tracking-tight text-ink mb-4">Upload Vendor Offer (Optional)</h2>
          <p className="text-sm text-gray-600 mb-4">
            Upload a PDF to automatically extract vendor details and order items
          </p>

          <div className="border-2 border-dashed border-gray-300 rounded-lg p-8 text-center hover:border-accent-deep/50 transition-colors">
            <input
              type="file"
              accept="application/pdf,.pdf"
              onChange={handleFileChange}
              className="hidden"
              id="pdf-upload"
              disabled={extracting}
            />
            <label htmlFor="pdf-upload" className="cursor-pointer">
              {extracting ? (
                <div className="flex flex-col items-center">
                  <Loader size="lg" />
                  <p className="mt-4 text-sm text-gray-600">Extracting data from PDF...</p>
                </div>
              ) : file ? (
                <div>
                  <svg
                    className="mx-auto h-12 w-12 text-green-500"
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M5 13l4 4L19 7"
                    />
                  </svg>
                  <p className="mt-2 text-sm font-medium text-gray-900">{file.name}</p>
                  <p className="mt-1 text-xs text-gray-500">Click to upload a different file</p>
                </div>
              ) : (
                <div>
                  <svg
                    className="mx-auto h-12 w-12 text-gray-400"
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"
                    />
                  </svg>
                  <p className="mt-2 text-sm text-gray-600">
                    Click to upload or drag and drop
                  </p>
                  <p className="mt-1 text-xs text-gray-500">PDF up to 10MB</p>
                </div>
              )}
            </label>
          </div>
        </div>

        {/* Request Details */}
        <div className="lio-card p-6 space-y-4">
          <h2 className="text-xl font-semibold tracking-tight text-ink mb-4">Request Details</h2>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              {fieldLabel('Requestor Name *', 'requestorName')}
              <Input
                aria-label="Requestor Name"
                value={requestorName}
                onChange={(e) => setRequestorName(e.target.value)}
                required
                placeholder="John Doe"
              />
            </div>

            <div>
              {fieldLabel('Department', 'department')}
              <Input
                aria-label="Department"
              value={department}
              onChange={(e) => setDepartment(e.target.value)}
              placeholder="IT Department"
            />
            </div>
          </div>

          <div>
            {fieldLabel('Title / Short Description *', 'title')}
            <Input
              aria-label="Title / Short Description"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
              placeholder="Adobe Creative Cloud Subscription"
            />
          </div>
        </div>

        {/* Vendor Information */}
        <div className="lio-card p-6 space-y-4">
          <h2 className="text-xl font-semibold tracking-tight text-ink mb-4">Vendor Information</h2>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              {fieldLabel('Vendor Name *', 'vendorName')}
              <Input
                aria-label="Vendor Name"
              value={vendorName}
              onChange={(e) => setVendorName(e.target.value)}
              required
              placeholder="Adobe Systems"
            />
            </div>

            <div>
              {fieldLabel('Tax ID', 'vatId')}
              <Input
                aria-label="Tax ID"
              value={vatId}
              onChange={(e) => setVatId(e.target.value)}
              placeholder="12-3456789"
            />
            </div>

            <div>
              {fieldLabel('Quote number', 'quoteNumber')}
              <Input
                aria-label="Quote number"
              value={quoteNumber}
              onChange={(e) => setQuoteNumber(e.target.value)}
              placeholder="Q-2024-001"
            />
            </div>

            <div>
              {fieldLabel('Valid until', 'validUntil')}
              <Input
                aria-label="Valid until"
              type="date"
              value={validUntil}
              onChange={(e) => setValidUntil(e.target.value)}
            />
            </div>
          </div>

          <div>
            <div className="mb-1.5 flex h-5 items-center justify-between">
              <span className="text-sm font-medium text-ink/70">Primary commodity group</span>
              {fileUrl && primaryGroupBoxes.length > 0 && (
                <SourceButton
                  className="h-6 w-6 -my-0.5"
                  label="Show the lines booked to this group in document"
                  onClick={() => docPanel.current?.showSource({ sources: primaryGroupBoxes })}
                />
              )}
            </div>
            <p className="text-sm text-ink">
              {primaryGroupLabel ?? (
                <span className="text-ink/45">Set a group on the order lines below</span>
              )}
            </p>
            <p className="mt-1 text-xs text-ink/45">
              Booked per line; the request shows the group with the largest spend.
            </p>
          </div>
        </div>

        {/* Order Lines */}
        <div className="lio-card p-6">
          <h2 className="text-xl font-semibold tracking-tight text-ink mb-4">Order Lines *</h2>
          <WarningList
            className="mb-4"
            warnings={extractionWarnings}
            onShowSource={fileUrl ? (sources) => docPanel.current?.showSource({ sources }) : undefined}
          />
          {otherSuppliers.length > 0 && (
            <p className="mb-4 rounded-lg bg-ink-800/[0.04] px-3 py-2 text-sm text-ink/70">
              This request now includes catalog lines from {otherSuppliers.join(', ')}, not only {vendorName || 'the quote vendor'}.
            </p>
          )}
          {catalogLoading && (
            <p className="mb-3 text-sm text-ink/45">Looking for matching catalog articles…</p>
          )}
          <OrderLinesEditor
            renderLineExtra={(line, isEditing) => (
              <CatalogSuggestion
                line={line}
                editable={isEditing}
                onRestore={() => {
                  setOrderLines((ls) => ls.map((l) => l.key === line.key ? restoreArticle(l) : l));
                }}
                suggestions={catalog[line.key]}
                applied={line.articleId ? appliedArticles[line.articleId] : undefined}
                onApply={(s, amount) => {
                  setAppliedArticles((a) => ({ ...a, [s.articleId]: s }));
                  setOrderLines((ls) => ls.map((l) => (l.key === line.key ? applyArticle(l, s, amount) : l)));
                }}
              />
            )}
            groups={commodityGroups}
            lines={orderLines}
            taxTotal={taxTotal}
            editable
            quotedGrandTotal={quotedGrandTotal}
            onShowTotal={
              fileUrl && fieldSources.totalCost?.length
                ? () => docPanel.current?.showSource({ sources: fieldSources.totalCost })
                : undefined
            }
            onChange={(lines, tax) => {
              setOrderLines(lines);
              setTaxTotal(tax);
            }}
            onShowSource={fileUrl ? (line) => docPanel.current?.showSource(line) : undefined}
          />
        </div>

        {/* Submit */}
        <div className="flex items-center justify-end space-x-4">
          <Button
            type="button"
            variant="outline"
            onClick={() => router.back()}
          >
            Cancel
          </Button>
          <Button type="submit" size="lg" loading={submitting} disabled={extracting || !loaded}>
            Create Request
          </Button>
        </div>
        </fieldset>
      </form>
      {fileUrl && (
        <div className="xl:sticky xl:top-6">
          <DocumentPanel ref={docPanel} fileUrl={fileUrl} title={file?.name} />
        </div>
      )}
      </div>
    </div>
  );
}

