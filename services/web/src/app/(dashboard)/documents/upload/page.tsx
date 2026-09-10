'use client';

import React, { useEffect, useState, Suspense } from 'react';
import Link from 'next/link';
import { gatewayFetch } from '@/lib/gateway-client';
import {
  Upload,
  FileText,
  CheckCircle2,
  AlertCircle,
  ArrowLeft,
  Info,
} from 'lucide-react';

function UploadContent() {
  const [files, setFiles] = useState<File[]>([]);
  const [strategy, setStrategy] = useState('token');
  const [tags, setTags] = useState('');
  const [sourceDate, setSourceDate] = useState('');
  const [isUploading, setIsUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [uploadedDocuments, setUploadedDocuments] = useState<Array<{ id: string; name: string }>>([]);

  useEffect(() => {
    let cancelled = false;
    fetch('/api/settings', { credentials: 'same-origin' })
      .then(async (response) => (response.ok ? response.json() : null))
      .then((payload) => {
        const preferred = payload?.settings?.default_chunk_strategy;
        if (!cancelled && ['token', 'sentence', 'recursive'].includes(preferred)) {
          setStrategy(preferred);
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  const selectFiles = (selectedFiles: FileList | null) => {
    if (!selectedFiles) return;
    const selected = Array.from(selectedFiles);
    if (selected.length > 20) {
      setFiles([]);
      setErrorMsg('Select at most 20 files per batch.');
      return;
    }
    const oversized = selected.find((file) => file.size > 50 * 1024 * 1024);
    if (oversized) {
      setFiles([]);
      setErrorMsg(`${oversized.name} exceeds the 50 MB file limit.`);
      return;
    }
    setFiles(selected);
    setUploadedDocuments([]);
    setUploadProgress(0);
    setErrorMsg(null);
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    selectFiles(e.target.files);
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    selectFiles(e.dataTransfer.files);
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (files.length === 0) {
      setErrorMsg('Please select at least one file.');
      return;
    }

    setIsUploading(true);
    setUploadProgress(0);
    setErrorMsg(null);
    setUploadedDocuments([]);

    const completed: Array<{ id: string; name: string }> = [];
    const failures: string[] = [];
    for (const [index, file] of files.entries()) {
      try {
        const idempotencyKey = crypto.randomUUID();
        let acceptedDocumentId: string | undefined;
        let lastFailure = 'Failed to ingest document.';

        for (let attempt = 0; attempt < 2 && !acceptedDocumentId; attempt += 1) {
          const formData = new FormData();
          formData.append('file', file);
          formData.append('chunk_strategy', strategy);
          formData.append('tags', tags);
          if (sourceDate) formData.append('source_time', `${sourceDate}T00:00:00.000Z`);

          const response = await gatewayFetch('/documents/upload', {
            method: 'POST',
            headers: { 'idempotency-key': idempotencyKey },
            body: formData,
          }, { profile: 'processing' });
          const data: {
            document_id?: string;
            detail?: string | {
              code?: string;
              message?: string;
              document_id?: string;
            };
            message?: string;
          } = await response.json();
          const duplicateDocumentId = typeof data.detail === 'object'
            && ['duplicate_document', 'duplicate_document_version'].includes(data.detail.code || '')
            ? data.detail.document_id
            : undefined;
          if ((response.ok && data.document_id) || duplicateDocumentId) {
            acceptedDocumentId = data.document_id || duplicateDocumentId;
            break;
          }

          const detail = typeof data.detail === 'string' ? data.detail : data.detail?.message;
          lastFailure = detail || data.message || lastFailure;
          if (attempt === 0 && [502, 503, 504].includes(response.status)) continue;
          throw new Error(lastFailure);
        }
        if (!acceptedDocumentId) throw new Error(lastFailure);
        completed.push({ id: acceptedDocumentId, name: file.name });
      } catch (error) {
        failures.push(`${file.name}: ${error instanceof Error ? error.message : 'Upload failed.'}`);
      } finally {
        setUploadProgress(index + 1);
      }
    }
    setUploadedDocuments(completed);
    if (failures.length > 0) setErrorMsg(failures.join(' '));
    setIsUploading(false);
  };

  return (
    <div className="p-6 max-w-2xl mx-auto space-y-6">
      {/* Top Header */}
      <div>
        <Link
          href="/documents"
          className="inline-flex items-center gap-1 text-xs text-zinc-400 hover:text-white mb-2 transition-colors"
        >
          <ArrowLeft className="w-3 h-3" />
          Back to Documents
        </Link>
        <h1 className="text-xl font-semibold text-white tracking-tight">
          Upload Document
        </h1>
        <p className="text-xs text-zinc-400 mt-0.5">
          Parse PDF, DOCX, Markdown, or TXT files into bounded chunks for pgvector indexing.
        </p>
      </div>

      {uploadedDocuments.length > 0 ? (
        <div className="p-6 rounded-xl bg-zinc-950 border border-zinc-800 text-center space-y-3">
          <CheckCircle2 className="w-8 h-8 text-emerald-400 mx-auto" />
          <h2 className="text-base font-semibold text-white">
            {uploadedDocuments.length === 1 ? 'Document queued' : `${uploadedDocuments.length} documents queued`}
          </h2>
          <p className="text-xs text-zinc-400 max-w-md mx-auto">
            Exact originals were preserved and verified before parsing, chunking, and indexing.
          </p>
          <div className="space-y-1 text-left">
            {uploadedDocuments.map((document) => (
              <Link
                key={document.id}
                href={`/documents/${document.id}`}
                className="block rounded-md border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs text-zinc-300 hover:text-white"
              >
                {document.name}
              </Link>
            ))}
          </div>
          {errorMsg && (
            <div className="rounded-lg border border-red-900/50 bg-red-950/30 p-3 text-left text-xs text-red-300">
              Some files could not be uploaded: {errorMsg}
            </div>
          )}
          <div className="pt-2 flex justify-center gap-2">
            <Link
              href={`/documents/${uploadedDocuments[0].id}`}
              className="px-3.5 py-1.5 rounded-lg bg-white text-black hover:bg-zinc-200 text-xs font-medium transition-colors"
            >
              Inspect {uploadedDocuments.length === 1 ? 'Document' : 'First Document'}
            </Link>
            <button
              type="button"
              onClick={() => {
                setFiles([]);
                setUploadedDocuments([]);
                setUploadProgress(0);
                setErrorMsg(null);
                setSourceDate('');
              }}
              className="px-3.5 py-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 text-zinc-300 border border-zinc-800 text-xs font-medium transition-colors"
            >
              Upload Another
            </button>
          </div>
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-4">
          {errorMsg && (
            <div className="p-3 rounded-lg bg-zinc-900 border border-red-900/50 text-red-400 text-xs flex items-center gap-2">
              <AlertCircle className="w-4 h-4 shrink-0" />
              {errorMsg}
            </div>
          )}

          {/* Dropzone */}
          <div
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            className={`border border-dashed rounded-xl p-6 text-center transition-colors cursor-pointer ${
              files.length > 0
                ? 'border-zinc-500 bg-zinc-900/50'
                : 'border-zinc-800 hover:border-zinc-600 bg-zinc-950'
            }`}
            onClick={() => document.getElementById('file-upload-input')?.click()}
          >
            <input
              id="file-upload-input"
              type="file"
              multiple
              accept=".pdf,.docx,.md,.markdown,.txt"
              onChange={handleFileChange}
              className="hidden"
            />
            <FileText className="w-6 h-6 text-zinc-400 mx-auto mb-2" />
            {files.length > 0 ? (
              <div>
                <p className="text-xs font-medium text-white">
                  {files.length === 1 ? files[0].name : `${files.length} files selected`}
                </p>
                <p className="text-[11px] text-zinc-400 mt-0.5">
                  {(files.reduce((total, file) => total + file.size, 0) / 1024).toFixed(1)} KB total
                </p>
                <span className="inline-block mt-1 text-[11px] text-zinc-400">Click to change selection</span>
              </div>
            ) : (
              <div>
                <p className="text-xs font-medium text-zinc-300">
                  Drop files here or click to browse
                </p>
                <p className="text-[11px] text-zinc-500 mt-0.5">PDF, DOCX, Markdown, TXT (Max 50MB)</p>
              </div>
            )}
          </div>

          {/* Chunking Strategy */}
          <div className="space-y-1.5">
            <label className="text-xs text-zinc-300">Strategy</label>
            <div className="grid grid-cols-3 gap-2">
              {[
                { id: 'token', label: 'Token', desc: '400 tok / 50 ovlp' },
                { id: 'sentence', label: 'Sentence', desc: '450 tok / 40 ovlp' },
                { id: 'recursive', label: 'Recursive', desc: 'Para → Sentence' },
              ].map((strat) => (
                <div
                  key={strat.id}
                  onClick={() => setStrategy(strat.id)}
                  className={`p-2.5 rounded-lg border cursor-pointer transition-colors ${
                    strategy === strat.id
                      ? 'border-zinc-400 bg-zinc-900 text-white font-medium'
                      : 'border-zinc-800 bg-zinc-950 text-zinc-400 hover:bg-zinc-900/50'
                  }`}
                >
                  <div className="text-xs text-zinc-200">{strat.label}</div>
                  <div className="text-[10px] text-zinc-500 mt-0.5">{strat.desc}</div>
                </div>
              ))}
            </div>
          </div>

          {/* Tags */}
          <div className="space-y-1">
            <label className="text-xs text-zinc-300">Tags (comma-separated)</label>
            <input
              type="text"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="e.g. engineering, api, v1"
              className="w-full px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
            />
          </div>

          <div className="space-y-1">
            <label htmlFor="source-date" className="text-xs text-zinc-300">
              Source effective date (optional)
            </label>
            <input
              id="source-date"
              type="date"
              value={sourceDate}
              onChange={(event) => setSourceDate(event.target.value)}
              className="w-full px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 focus:outline-none focus:border-zinc-600"
            />
            <p className="text-[10px] text-zinc-500">
              Use only when the source explicitly applies to a known date. Certus records upload time separately.
            </p>
          </div>

          {/* Contextual Notice */}
          <div className="p-3 rounded-lg bg-zinc-900 border border-zinc-800 text-[11px] text-zinc-400 flex items-start gap-2">
            <Info className="w-3.5 h-3.5 text-zinc-400 shrink-0 mt-0.5" />
            <span>
              Contextual headers are automatically prepended to every chunk to improve hybrid RAG precision.
            </span>
          </div>

          {/* Submit Button */}
          <button
            type="submit"
            disabled={files.length === 0 || isUploading}
            className="w-full py-2 rounded-lg bg-white hover:bg-zinc-200 disabled:opacity-40 text-black text-xs font-medium transition-colors flex items-center justify-center gap-1.5"
          >
            {isUploading ? (
              <span>Uploading {uploadProgress + 1} of {files.length}...</span>
            ) : (
              <>
                <Upload className="w-3.5 h-3.5" />
                <span>Ingest {files.length === 1 ? 'Document' : `${files.length} Documents`}</span>
              </>
            )}
          </button>
        </form>
      )}
    </div>
  );
}

export default function DocumentUploadPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-xs text-zinc-500">Loading Upload...</div>}>
      <UploadContent />
    </Suspense>
  );
}
