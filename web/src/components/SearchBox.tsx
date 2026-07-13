"use client";

import { useState } from "react";

interface SearchBoxProps {
  matchCount: number | null;
  loading: boolean;
  onSearch: (q: string) => void;
  onClear: () => void;
}

export function SearchBox({
  matchCount,
  loading,
  onSearch,
  onClear,
}: SearchBoxProps) {
  const [value, setValue] = useState("");

  const submit = () => {
    const q = value.trim();
    if (q) onSearch(q);
    else onClear();
  };

  return (
    <div>
      <h3 className="text-xs font-medium text-stone-400 uppercase tracking-wide mb-2">
        Search the Map
      </h3>
      <p className="text-xs text-stone-500 mb-2">
        Highlights matching chunks. Try &quot;anti-rent&quot;, &quot;Steele&quot;,
        &quot;Mexico&quot;.
      </p>
      <div className="flex gap-1">
        <input
          type="search"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
          placeholder="keyword..."
          className="flex-1 min-w-0 px-2 py-1 rounded bg-stone-800 border border-stone-700 text-stone-200 text-sm placeholder:text-stone-600 focus:outline-none focus:border-amber-700"
        />
        <button
          onClick={submit}
          disabled={loading}
          className="px-3 py-1 rounded bg-amber-800 text-amber-50 text-xs font-medium hover:bg-amber-700 disabled:opacity-50"
        >
          {loading ? "..." : "Find"}
        </button>
        {matchCount !== null && (
          <button
            onClick={() => {
              setValue("");
              onClear();
            }}
            className="px-2 py-1 rounded text-xs text-stone-400 hover:text-stone-200 border border-stone-700"
          >
            Clear
          </button>
        )}
      </div>
      {matchCount !== null && (
        <p className="text-xs text-stone-400 mt-1.5">
          {matchCount.toLocaleString()} match{matchCount === 1 ? "" : "es"}
        </p>
      )}
    </div>
  );
}
