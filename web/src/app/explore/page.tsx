"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";

// The /explore topic map. Reuses the standalone canvas renderer
// (public/cluster_map.html) in an iframe: we fetch the latest snapshot from
// /api/map and hand it to the frame via postMessage once it signals ready.
export default function Explore() {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const mapRef = useRef<unknown>(null);
  const readyRef = useRef(false);
  const [status, setStatus] = useState<"loading" | "empty" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);

  useEffect(() => {
    function tryPost() {
      if (readyRef.current && mapRef.current && iframeRef.current?.contentWindow) {
        iframeRef.current.contentWindow.postMessage(
          { __map: true, data: mapRef.current },
          "*"
        );
      }
    }

    function onMessage(e: MessageEvent) {
      if (e.data && e.data.__mapReady) {
        readyRef.current = true;
        tryPost();
      }
    }
    window.addEventListener("message", onMessage);

    fetch("/api/map")
      .then(async (r) => {
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || `Request failed (${r.status})`);
        if (d.map) {
          mapRef.current = d.map;
          setGeneratedAt(d.generated_at ?? null);
          setStatus("ready");
          tryPost();
        } else {
          setStatus("empty");
        }
      })
      .catch((e) => {
        setError(e instanceof Error ? e.message : "Failed to load the map");
        setStatus("error");
      });

    return () => window.removeEventListener("message", onMessage);
  }, []);

  return (
    <main className="fixed inset-0 flex flex-col">
      <header className="flex items-center justify-between border-b border-stone-200 bg-[#faf7f0] px-4 py-2">
        <div className="flex items-baseline gap-3">
          <Link href="/" className="text-sm text-amber-800 hover:underline">
            ← Ask
          </Link>
          <span className="font-serif text-lg">Topic map</span>
          {generatedAt && (
            <span className="text-xs text-stone-400">
              generated {generatedAt.slice(0, 10)}
            </span>
          )}
        </div>
      </header>

      <div className="relative flex-1">
        {status === "ready" && (
          <iframe
            ref={iframeRef}
            src="/cluster_map.html"
            title="Topic map"
            className="absolute inset-0 h-full w-full border-0"
          />
        )}
        {status === "loading" && (
          <div className="flex h-full items-center justify-center text-sm text-stone-500">
            Loading the map…
          </div>
        )}
        {status === "empty" && (
          <div className="mx-auto flex h-full max-w-md flex-col items-center justify-center px-6 text-center">
            <p className="font-serif text-xl">No map generated yet</p>
            <p className="mt-2 text-sm text-stone-600">
              Run the <code className="rounded bg-stone-200 px-1">cluster</code> workflow
              with <code className="rounded bg-stone-200 px-1">--publish</code> to build the
              topic map. It clusters the corpus and writes a snapshot the page reads.
            </p>
            <Link href="/" className="mt-4 text-sm text-amber-800 hover:underline">
              ← Back to Ask
            </Link>
          </div>
        )}
        {status === "error" && (
          <div className="flex h-full items-center justify-center px-6 text-center text-sm text-red-700">
            {error}
          </div>
        )}
      </div>
    </main>
  );
}
