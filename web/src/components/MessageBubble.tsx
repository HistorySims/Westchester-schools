"use client";

import { Fragment } from "react";
import type { Message, Citation } from "@/lib/types";
import { CitationLink } from "./CitationLink";

interface MessageBubbleProps {
  message: Message;
  activeCitationIndex: number | null;
  onCitationClick: (citation: Citation) => void;
}

function renderWithCitations(
  text: string,
  citations: Citation[],
  activeCitationIndex: number | null,
  onCitationClick: (citation: Citation) => void
) {
  const parts: (string | { citation: Citation })[] = [];
  const re = /\[(\d+)\]/g;
  let lastIndex = 0;
  let match;

  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const idx = parseInt(match[1], 10);
    const citation = citations.find((c) => c.index === idx);
    if (citation) {
      parts.push({ citation });
    } else {
      parts.push(match[0]);
    }
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return parts.map((part, i) => {
    if (typeof part === "string") {
      return <Fragment key={i}>{part}</Fragment>;
    }
    return (
      <CitationLink
        key={i}
        citation={part.citation}
        isActive={activeCitationIndex === part.citation.index}
        onClick={onCitationClick}
      />
    );
  });
}

export function MessageBubble({
  message,
  activeCitationIndex,
  onCitationClick,
}: MessageBubbleProps) {
  const isUser = message.role === "user";

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"} mb-4`}>
      <div
        className={`max-w-[85%] rounded-xl px-4 py-3 ${
          isUser
            ? "bg-amber-800 text-amber-50"
            : "bg-stone-100 text-stone-900 border border-stone-200"
        }`}
      >
        {isUser ? (
          <p className="text-sm">{message.content}</p>
        ) : (
          <div className="text-sm leading-relaxed font-serif whitespace-pre-wrap">
            {message.loading ? (
              <span>{message.content}<span className="animate-pulse">|</span></span>
            ) : message.citations && message.citations.length > 0 ? (
              renderWithCitations(
                message.content,
                message.citations,
                activeCitationIndex,
                onCitationClick
              )
            ) : (
              message.content
            )}
          </div>
        )}
        {!isUser && message.citations && message.citations.length > 0 && !message.loading && (
          <div className="mt-3 pt-3 border-t border-stone-200">
            <p className="text-xs font-sans text-stone-500 mb-1">Sources</p>
            <div className="space-y-1">
              {message.citations
                .filter((c) => {
                  const re = new RegExp(`\\[${c.index}\\]`);
                  return re.test(message.content);
                })
                .map((c) => (
                  <button
                    key={c.index}
                    onClick={() => onCitationClick(c)}
                    className={`block w-full text-left text-xs font-mono px-2 py-1 rounded transition-colors ${
                      activeCitationIndex === c.index
                        ? "bg-amber-100 text-amber-900"
                        : "text-stone-600 hover:bg-stone-50"
                    }`}
                  >
                    [{c.index}] {c.paper_title}, {c.date_issued}, p.
                    {c.page_sequence}
                  </button>
                ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
