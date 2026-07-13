const VOYAGE_BASE = "https://api.voyageai.com/v1";
const EMBED_MODEL = "voyage-3.5";
const RERANK_MODEL = "rerank-2.5";
const EMBED_DIMS = 1024;

function getApiKey(): string {
  const key = process.env.VOYAGE_API_KEY;
  if (!key) throw new Error("Missing VOYAGE_API_KEY");
  return key;
}

async function voyageFetch(path: string, body: object): Promise<Response> {
  const apiKey = getApiKey();
  const maxRetries = 3;
  let delay = 1000;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    const resp = await fetch(`${VOYAGE_BASE}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify(body),
    });
    if (resp.status === 429 || resp.status >= 500) {
      if (attempt >= maxRetries) return resp;
      await new Promise((r) => setTimeout(r, delay));
      delay *= 2;
      continue;
    }
    return resp;
  }
  throw new Error("voyage fetch loop ended unexpectedly");
}

export async function embedQuery(text: string): Promise<number[]> {
  const resp = await voyageFetch("/embeddings", {
    model: EMBED_MODEL,
    input: [text],
    input_type: "query",
    output_dimension: EMBED_DIMS,
  });
  if (!resp.ok) {
    throw new Error(`Voyage embed failed: ${resp.status} ${await resp.text()}`);
  }
  const data = await resp.json();
  return data.data[0].embedding;
}

export async function embedQueries(texts: string[]): Promise<number[][]> {
  if (texts.length === 0) return [];
  const resp = await voyageFetch("/embeddings", {
    model: EMBED_MODEL,
    input: texts,
    input_type: "query",
    output_dimension: EMBED_DIMS,
  });
  if (!resp.ok) {
    throw new Error(`Voyage embed failed: ${resp.status} ${await resp.text()}`);
  }
  const data = await resp.json();
  return (data.data as { embedding: number[] }[]).map((d) => d.embedding);
}

export async function rerank(
  query: string,
  documents: string[],
  topK: number = 20
): Promise<{ index: number; relevance_score: number }[]> {
  if (documents.length === 0) return [];
  const resp = await voyageFetch("/rerank", {
    model: RERANK_MODEL,
    query,
    documents,
    top_k: topK,
  });
  if (!resp.ok) {
    throw new Error(
      `Voyage rerank failed: ${resp.status} ${await resp.text()}`
    );
  }
  const data = await resp.json();
  return data.data;
}
