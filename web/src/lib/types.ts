export interface ChunkRow {
  chunk_id: string;
  content: string;
  page_id: string;
  paper_lccn: string;
  paper_title: string;
  date_issued: string;
  edition: number;
  page_sequence: number;
  image_url: string;
  resource_url: string;
}

export interface SemanticResult extends ChunkRow {
  similarity: number;
}

export interface FtsResult extends ChunkRow {
  rank: number;
}

export interface RankedChunk extends ChunkRow {
  rrf_score: number;
  rerank_score?: number;
}

export interface Citation {
  index: number;
  chunk_id: string;
  paper_title: string;
  paper_lccn: string;
  date_issued: string;
  page_sequence: number;
  edition: number;
  image_url: string;
  resource_url: string;
  snippet: string;
}

export interface AskResponse {
  text: string;
  citations: Citation[];
  refused: boolean;
  input_tokens: number;
  output_tokens: number;
}

export type ResponseMode = "synthesis" | "research" | "directory";

export interface AskRequest {
  question: string;
  mode?: ResponseMode;
  paper_lccn?: string | null;
  date_from?: string | null;
  date_to?: string | null;
  scope_tier?: number | null;
  scope_label?: number | null;
}

export interface Message {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  refused?: boolean;
  loading?: boolean;
}
