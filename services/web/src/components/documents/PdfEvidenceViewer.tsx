'use client';

import { useEffect, useRef, useState } from 'react';
import type { PDFDocumentLoadingTask, PDFDocumentProxy, RenderTask } from 'pdfjs-dist';
import { rotateCropboxPoint } from './pdfGeometry';

export interface PdfGlyphSelector {
  type: 'certus:PdfGlyphSelector';
  coordinate_system: 'pymupdf_unrotated_cropbox_top_left_points:v1';
  offset_unit: 'unicodeCodePoint';
  range_semantics: 'zeroBasedHalfOpen';
  granularity: 'nativeGlyphQuad';
  pages: Array<{
    page_index: number;
    page_label: string;
    width_points: number;
    height_points: number;
    rotation_degrees: 0 | 90 | 180 | 270;
    crop_box: [number, number, number, number];
    runs: Array<{
      parsed_start: number;
      parsed_end: number;
      reading_order: number;
      glyphs: Array<{
        parsed_start: number;
        parsed_end: number;
        quad: [number, number, number, number, number, number, number, number];
      }>;
    }>;
  }>;
}

interface PdfEvidenceViewerProps {
  sourceUrl: string;
  selector: PdfGlyphSelector;
}

interface RenderedPageProps {
  pdf: PDFDocumentProxy;
  selection: PdfGlyphSelector['pages'][number];
}

function RenderedPage({ pdf, selection }: RenderedPageProps) {
  const pageCanvasRef = useRef<HTMLCanvasElement>(null);
  const highlightCanvasRef = useRef<HTMLCanvasElement>(null);
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 });
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let renderTask: RenderTask | null = null;

    async function renderPage() {
      const page = await pdf.getPage(selection.page_index + 1);
      if (cancelled) return;
      const maxCssWidth = 820;
      const unrotatedViewport = page.getViewport({ scale: 1, rotation: 0 });
      if (
        Math.abs(unrotatedViewport.width - selection.width_points) > 0.5
        || Math.abs(unrotatedViewport.height - selection.height_points) > 0.5
      ) {
        throw new Error('The rendered PDF page dimensions do not match the verified layout artifact.');
      }
      const baseViewport = page.getViewport({ scale: 1, rotation: selection.rotation_degrees });
      const scale = Math.min(1.6, maxCssWidth / baseViewport.width);
      const viewport = page.getViewport({ scale, rotation: selection.rotation_degrees });
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      const pageCanvas = pageCanvasRef.current;
      const highlightCanvas = highlightCanvasRef.current;
      if (!pageCanvas || !highlightCanvas || cancelled) return;

      for (const canvas of [pageCanvas, highlightCanvas]) {
        canvas.width = Math.ceil(viewport.width * pixelRatio);
        canvas.height = Math.ceil(viewport.height * pixelRatio);
        canvas.style.width = `${viewport.width}px`;
        canvas.style.height = `${viewport.height}px`;
      }
      setDimensions({ width: viewport.width, height: viewport.height });
      const pageContext = pageCanvas.getContext('2d', { alpha: false });
      const highlightContext = highlightCanvas.getContext('2d');
      if (!pageContext || !highlightContext) throw new Error('Canvas rendering is unavailable.');

      renderTask = page.render({
        canvas: pageCanvas,
        canvasContext: pageContext,
        viewport,
        transform: pixelRatio === 1 ? undefined : [pixelRatio, 0, 0, pixelRatio, 0, 0],
      });
      await renderTask.promise;
      if (cancelled) return;

      highlightContext.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      highlightContext.fillStyle = 'rgba(250, 204, 21, 0.30)';
      highlightContext.strokeStyle = 'rgba(250, 204, 21, 0.88)';
      highlightContext.lineWidth = 0.8;
      for (const run of selection.runs) {
        for (const glyph of run.glyphs) {
          const points = [
            [glyph.quad[0], glyph.quad[1]],
            [glyph.quad[2], glyph.quad[3]],
            [glyph.quad[6], glyph.quad[7]],
            [glyph.quad[4], glyph.quad[5]],
          ].map(([x, y]) => rotateCropboxPoint(
            x,
            y,
            selection.width_points,
            selection.height_points,
            selection.rotation_degrees,
          ));
          highlightContext.beginPath();
          highlightContext.moveTo(points[0][0] * scale, points[0][1] * scale);
          for (const [x, y] of points.slice(1)) highlightContext.lineTo(x * scale, y * scale);
          highlightContext.closePath();
          highlightContext.fill();
          highlightContext.stroke();
        }
      }
    }

    void renderPage().catch((renderError: unknown) => {
      if (!cancelled) {
        setError(renderError instanceof Error ? renderError.message : 'PDF page could not be rendered.');
      }
    });
    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [pdf, selection]);

  if (error) {
    return <div role="alert" className="rounded-lg border border-red-900/60 bg-red-950/30 p-3 text-red-300">{error}</div>;
  }

  return (
    <figure className="space-y-2">
      <figcaption className="flex items-center justify-between text-[11px] text-zinc-400">
        <span>PDF page {selection.page_label}</span>
        <span>{selection.runs.reduce((count, run) => count + run.glyphs.length, 0)} verified glyphs</span>
      </figcaption>
      <div
        className="relative mx-auto overflow-hidden rounded-md bg-white shadow-xl ring-1 ring-zinc-700"
        style={{ width: dimensions.width || undefined, height: dimensions.height || undefined }}
      >
        <canvas ref={pageCanvasRef} aria-label={`Rendered PDF page ${selection.page_label}`} />
        <canvas
          ref={highlightCanvasRef}
          aria-hidden="true"
          className="pointer-events-none absolute inset-0"
        />
      </div>
    </figure>
  );
}

export function PdfEvidenceViewer({ sourceUrl, selector }: PdfEvidenceViewerProps) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let loadingTask: PDFDocumentLoadingTask | null = null;
    async function loadPdf() {
      const pdfjs = await import('pdfjs-dist');
      pdfjs.GlobalWorkerOptions.workerSrc = new URL(
        'pdfjs-dist/build/pdf.worker.min.mjs',
        import.meta.url,
      ).toString();
      loadingTask = pdfjs.getDocument({
        url: sourceUrl,
        withCredentials: true,
        enableXfa: false,
        stopAtErrors: true,
        maxImageSize: 40_000_000,
        canvasMaxAreaInBytes: 64 * 1024 * 1024,
        disableRange: true,
      });
      const loaded = await loadingTask.promise;
      if (!cancelled) setPdf(loaded);
    }
    void loadPdf().catch((loadError: unknown) => {
      if (!cancelled) {
        setError(loadError instanceof Error ? loadError.message : 'The exact PDF could not be loaded.');
      }
    });
    return () => {
      cancelled = true;
      if (loadingTask) void loadingTask.destroy();
    };
  }, [sourceUrl]);

  if (error) {
    return <div role="alert" className="rounded-lg border border-red-900/60 bg-red-950/30 p-3 text-red-300">{error}</div>;
  }
  if (!pdf) return <p className="text-zinc-400">Loading exact PDF proof…</p>;

  return (
    <div className="max-h-[72vh] space-y-5 overflow-auto rounded-lg border border-zinc-800 bg-zinc-950 p-3">
      {selector.pages.map((page) => (
        <RenderedPage key={page.page_index} pdf={pdf} selection={page} />
      ))}
    </div>
  );
}
